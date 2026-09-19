# Dashboard observability verification

Issue #2101 adds the read-only `STRONG_AUDIT` and `ORDINARY_CLOSURE` reviewer
execution boundary consumed by the existing two-tier lifecycle. It introduces no
production dispatch wiring, trace emission, processing origin/outcome, structured
event schema, or dashboard projection: `pr_review_execution.py` returns validated
evidence but has no publication or merge authority. The production integration
that schedules these roles must add the corresponding production-to-view trace
coverage. Run `bash scripts/test.sh tests/test_pr_review_execution.py` for prompt,
portable-bundle, identity, disposition-completeness, and cumulative-scope boundary
coverage.

GitHub webhook-driven cache eviction and expedited CI watch recheck (PR #2119)
improve the turnaround time from CI completion to validation launch. Upon webhook intake,
matching HTTP cache entries in Hishel SQLite storage are evicted, CI observation requests
pass `Cache-Control: no-cache` to prevent reading stale checks, and when PR Actions checks
are in progress the PR is marked DEFERRED with a 30-second recheck scheduled on its active
CI watch. This is an admission-timing and cache-invalidation optimization; it introduces
no new production trace stage, origin, provider route, structured event schema, or dashboard
projection. The existing PR processing trace reports `PRProcessingOutcome.DEFERRED` with the
action message "GitHub Actions checks are still in progress for PR #<number>, skipping to next PR".
Production-boundary tests in `tests/test_gh_cache_eviction.py`, `tests/test_entity_invalidation.py`,
and `tests/test_pr_processor.py` cover cache eviction, CI watch recheck scheduling, and
`_handle_pr_merge` behavior; run `bash scripts/test.sh tests/test_gh_cache_eviction.py tests/test_entity_invalidation.py tests/test_pr_processor.py`.

The generation-aware Issue routing store is a pre-worker durability boundary:
it classifies and orders Review and Implementation lane records but does not yet
execute either lane or emit a processing result. Consequently it introduces no
new production trace stage, origin, outcome, provider route, structured event,
or dashboard projection. `tests/test_issue_stage_routing.py::test_invalidation_and_startup_recovery_route_authoritative_standalone_decision`
drives the real durable invalidation worker and startup enumeration through
authoritative GitHub refresh and lifecycle-decision reads, then verifies the
durable Review-to-Implementation handoff and restart-preserved arrival. The
same test module supplements that production boundary with classification,
coalescing, supersession, and owned-start unit coverage. The Review and
Implementation worker changes
which consume these records must add their production-to-view trace coverage;
the routing store must not fabricate worker activity before that handoff exists.
Run `bash scripts/test.sh tests/test_issue_stage_routing.py` for this boundary.
That production suite also covers partial family validation ending in ERROR,
departed-child membership cleanup, and offline closure followed by reopening;
these routing-only state transitions remain intentionally absent from the
execution timeline until a stage worker actually claims them.
Dependency-cache waits now refresh semantic routing before emitting the existing
`issue.cached-dependency-wait` event. The event schema and operational meaning
are unchanged: it still reports only an Implementation prerequisite wait and
does not claim Review execution, READY evidence, or provider admission.
The final post-processing routing refresh likewise emits no additional stage:
it consumes durable validation evidence for lane bookkeeping, while the existing
validation and implementation-admission events remain the observable outcomes.
Issue #2081 corrected an earlier defect in this same area: an
execution-routing-only change (configured backend, alias, model, fallback
order/membership, quota-based selection) no longer rebinds the engine-owned
`SpecificationValidationLifecycle`/`DecompositionValidationLifecycle`
instances, no longer changes `ValidationIdentity`/`DecompositionIdentity`,
and is not interpreted as a new Review arrival or Implementation generation.
`AutomationEngine._get_specification_validator`/`_get_decomposition_validator`
now cache one lifecycle per repository unconditionally for the process
lifetime; execution routing is read fresh only inside `decide()`, once per
freshly model-computed decision, purely to record as diagnostic execution
provenance (`ValidationDecision.execution_provenance` /
`DecompositionDecision.execution_provenance`), never to gate reuse. This is
observability-neutral for the dashboard's trace stages, origins, outcomes,
and event schemas: no new stage, origin, outcome, or event is introduced, and
`evaluation_source` keeps the same three values (`model`,
`local-only`, `stored-decision-reuse`) it already had for individual
validation. `DecompositionDecision` did not previously carry an
`evaluation_source` field at all, so `issue.decomposition-validation-job`
facts always reported the generic `getattr(...)` fallback `"unrecorded"`;
decomposition decisions now report their real evaluation source the same
way individual decisions already did (mirrored for REQ-009 symmetry), which
is a genuine, intentional improvement to that job's observed facts, not a
regression to guard against. What otherwise changed is durable *reuse
eligibility* and *identity*, not the dashboard's stages, origins, or event
schemas. The production routing suite in
`tests/test_issue_stage_routing.py` (see
`test_running_engine_retains_standalone_classification_across_provider_policy_change`
and `test_running_engine_retains_both_family_categories_across_policy_change`)
covers backend/model changes and exact restoration for standalone and family
work, proving zero additional backend invocations and no Review requeue.
`tests/test_specification_validation_lifecycle.py` and
`tests/test_decomposition_validation_lifecycle.py` cover execution
provenance capture/preservation, an in-flight route change during an
analyzer call, and retained-but-non-authoritative legacy on-disk records
from before this migration (`legacy_policy_unproven`). Run
`bash scripts/test.sh tests/test_issue_stage_routing.py tests/test_specification_validation_lifecycle.py tests/test_decomposition_validation_lifecycle.py`
for this boundary.

Issue #2061 binds each durable Implementation generation (`issue_stage_routing.py`)
to the existing production implementation-ownership authority
(`ImplementationSlotRepository`, `implementation_slots.json`) via the new
`implementation_ownership.py` adapter, ahead of the dedicated Implementation
worker migration (#2055) that will later drain the Implementation lane
directly. This is an admission-gate and durable-resumption change, not a new
processing origin, outcome, provider route, or structured event: the real
admission boundary in `AutomationEngine._process_single_candidate_unified`
(and the stale-Jules-provider-recovery boundary in
`issue_processor.handle_stale_jules_issue_sessions`) already reported
`ExplicitTargetOutcome.SKIPPED`/`DEFERRED` for every other admission refusal
before this change; a generation already durably owned now reports the same
existing `SKIPPED` outcome with the action text "Skipped - Implementation
generation already has a durable production start" instead of falling
through to a capacity/duplicate-execution refusal, and a superseded or
ambiguous binding reports the same existing `DEFERRED` outcome family. No new
execution-trace stage, event kind, or dashboard projection is introduced. The
one new durable field this adds — `implementation_generation` on an
`ImplementationSlotRepository` owner record — is adapter-internal bookkeeping
analogous to the pre-existing, likewise unexposed `validation_identity`
field on the same owner record; it is not surfaced in
`ImplementationOwnerSnapshot` or the Implementation Slots panel, consistent
with that existing field.
Production-boundary regressions for this handoff, including crash-before-
acquisition, persisted-execution acquisition, supersession/exact-reversion,
ambiguous/legacy bindings, and stale-provider-recovery continuation, live in
`tests/test_implementation_ownership.py`; run
`bash scripts/test.sh tests/test_implementation_ownership.py` for this boundary.

The detail view is a projection of **observed local evidence**. It does not query
GitHub or a provider and it does not turn absent, unavailable, partial, throttled,
or superseded evidence into a pass or failure. An accepted provider submission is
a handoff, not proof that a pull request was published or that implementation
completed. Each top-level evaluation or durable resumption has a distinct execution
identity; late evidence remains attached to the execution scope that produced it.

Inherited specification BLOCKED publication withdraws an explicitly submitted
child's readiness while preserving the parent's readiness. The existing blocked outcome
and incomplete-publication reporting remain authoritative; no new origin,
provider route, or event schema is introduced. Readiness completion covers only
the child label; the child BLOCKED decision still denies its dispatch. Dashboard consumers must not infer label mutation from a BLOCKED
verdict alone. Run
`bash scripts/test.sh tests/test_specification_validation_lifecycle.py tests/test_validation_publication_resumption.py`
for the production lifecycle regressions, including
`test_inherited_blocked_preserves_parent_with_restart_retry` and
`test_inherited_blocked_edit_after_comment_preserves_both_labels`.

## Updating observable processing

Codex Cloud adversarial redelivery now derives each finding's remediation generation
from the first WHAM completed assistant turn chronologically ordered after that
finding's durable accepted-follow-up baseline. Observation occurs before a new
validation, and the accepted validation-to-generation association is durable;
replaying an older validation performs no new provider observation. This changes provider observation and restart
recovery, but not the existing `Cloud Task Adversarial Feedback` trace stage,
outcome, or fields: only a successful follow-up emits that event, while duplicate
or not-yet-completed generations remain action diagnostics. Pre-publication
snapshot persistence is a causal durability gate and does not emit a success event;
recovering an earlier provider receipt likewise cannot emit or satisfy a later
generation's delivery. The production-path
regression
`tests/test_adversarial_validation_pr_flow.py::TestAdversarialValidationCodexFeedback`
reconstructs clients across the durable boundary and proves one emission-worthy
delivery for a completed turn, immutable rejected-generation retry, per-finding
batch baselines, and suppression for replayed validations and provenance-only
activity. Run it
with `bash scripts/test.sh tests/test_codex_wham_client.py tests/test_adversarial_validation_pr_flow.py`.

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
declarations against validated, cache-aware GitHub reads. Its facts identify
`discovery_source=cached-open-issue-list`,
`live_scope=related-declarations-and-native-children`,
`declared_issue_numbers`, `discovery_payload=issue-bodies`, and `authorizes_execution=false`. The one-hour list
cache discovers candidates; family confirmation uses HTTP freshness and
revalidation rather than bypassing valid cached responses.
This completed stage does not claim complete live repository discovery or
implementation readiness. The production-path regression
`tests/test_parent_issue_reconciliation.py::test_family_discovery_refreshes_only_related_issues_and_records_scope`
checks both the bounded GitHub calls and the actual collector event; stale
declarations and uncached native-member conflicts are covered in the same file.
`tests/test_dashboard_observability.py::test_family_discovery_scope_reaches_mounted_detail`
joins that production event to the mounted detail view without issuing any
repository-wide strict discovery request.
The `--only` startup pass emits `issue.explicit-relationship-discovery`
with `discovery_source=cache-aware-open-issue-list`, `discovery_payload=issue-bodies`,
`live_scope=target-and-related-family`, and `authorizes_execution=false`.
Both discovery events carry `relationship_reads=http-cache-freshness`.
The source describes a cache policy rather than an unconditional cache hit.
`tests/test_dashboard_observability.py::test_explicit_cached_discovery_reaches_mounted_detail`
checks the production event and mounted detail view.
`tests/test_parent_issue_reconciliation.py::test_only_parent_reconciles_every_declared_child_before_unified_processing`
runs the real explicit-entry/decomposition/family path with both warm and cold
caches, rejects any enriched Issue-list or PR-connection call, and proves that
related declarations are materialized without refreshing unrelated Issues.
The mounted family and explicit detail tests also assert `discovery_payload=issue-bodies`.
`tests/test_only_discovery_cache.py` verifies the memory TTL boundary, repository
isolation, and paginated cache-backed discovery without enrichment. It also drives
the real HTTP-cache factory with a controlled wire transport: fresh persistent
responses avoid wire admission, while expired responses return to that boundary.
The private cache isolates credential variants and preserves wire timeout and
operation identity; local cache results remain diagnostic-only quota evidence.
The streaming GraphQL regressions in `tests/test_only_discovery_cache.py` exercise
this same production boundary: classification precedes admission, refused work
never reaches the wire, and accepted bodies remain intact. These transport
diagnostics do not create an implementation-ready or completed Issue event.

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
3. Update this guide and the relevant fragment(s) under `docs/client-features/`,
   or state a concrete reason in the PR description why the change is
   observability-neutral.
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

Changed-file evidence completion retains the existing
`pr.adversarial-validation` stage and processing origin. Its non-PASS event now
preserves `diagnostic_category` and `diagnostic_reason`, allowing the generic
Observed Evidence panel to distinguish unavailable completion sessions,
same-session discontinuity, invalid or contradictory path accounting,
absence-only IRRELEVANT rejections, evidence incompleteness, and stale
validation snapshots without coercing any of them into success. The completion
round now also rejects a response whose backend fell back to a fresh session
instead of resuming the original reviewer session
(`changed_file_completion_session_discontinuity`) and merges a later round's
newly recovered paths with evidence already recovered in an earlier round
instead of discarding it, so a focused completion cannot invalidate prior
same-snapshot recovery. `_apply_coverage_and_verdict_precedence` separately
rejects an IRRELEVANT classification justified only by the evidence being
unavailable (`absence_only_irrelevance_rejected`), and the evidence-recovery
parser rejects a duplicate/contradictory path before any verdict is computed.
Revalidating a snapshot before authorizing a completion-round PASS now uses
genuinely cache-bypassing retrieval for the diff, changed-file listing, and
linked Issue body (`bypass_cache=True`) instead of the general HTTP cache, and
the validation snapshot digest now also binds the raw diff content itself
(not just the authoritative REST listing), so a changed raw diff for an
already-decided file invalidates the snapshot even when the REST listing of
omitted files is unchanged. A path the initial round left UNAVAILABLE may
still transition to RECOVERED (or reveal a new finding) in the completion
round without being treated as a rejected contradiction; only a path already
RECOVERED/IRRELEVANT before that round must stay stable, and that gap is now
also retained diagnostically (in `summary`/`diagnostic_reason`) even when a
demonstrated finding or material test-oracle gap determines the top-level
NEEDS_FIX/NEEDS_TESTS verdict, instead of being silently dropped once a
higher-precedence verdict wins. Rather than trying to enumerate every way of
asserting absence-only irrelevance (an unwinnable paraphrase arms race — a
bare "This path is irrelevant." offers no absence clause to even match),
`_lacks_independent_irrelevance_scope_basis` instead requires an affirmative,
recognized *verification* marker to be present at all — a citation,
comparison, or verification action (e.g. a `.gitattributes` marker, or an
explicit "confirmed via"/"identical to" citation). A bare file-category
label alone (e.g. a generated/vendored/binary-asset reference, "This is
generated code.") does not count: it is itself just as bare an assertion as
"this path is irrelevant", so only a verification marker actually grants
basis, though category labels are still recognized so their own wording gets
masked out of the negation search below. A verification action word
("confirmed", "verified", "cross-referenced", "out of scope") likewise only
grants basis when its own preposition (via/by/to/from/per) is actually
followed by a concrete object — "Already reviewed." names no such object, so
it no longer counts either, and moved into the (masked-only) category
bucket alongside the file-category labels. A positive lookahead confirms that
the preposition has an object without consuming it. Consequently, in
"Verified by no independent evidence.", the complete word "no" remains visible
to the later `\bno\b` negation check. It checks each verification match
against the negation cues in its own clause — on either side of the marker,
so "no `.gitattributes` ... was obtained" and "the `.gitattributes` file was
not obtained" are both caught — so a marker invoked only to say it was NOT
obtained does not count as affirmative evidence. Because a recognized marker
can itself contain a generic negation word as an idiom (e.g. "no reviewable
logic"), every matched span is masked out of the text before that negation
search runs, so one marker's own wording can never be misread as negating a
*different* marker sharing the same clause; anything else is rejected
regardless of phrasing.

A persistent failure on one changed-file REST page no longer blocks
validation before the reviewer ever runs: the successfully retrieved page's
evidence is carried into context and reaches the bounded completion round
alongside an explicit `unresolvable_file_count`, and `pass_with_unresolvable_changed_file_count`
still fails closed at final verdict precedence so that gap can never
authorize PASS. Both the cached and cache-bypassing changed-file pagination
recognize any `GitHubRequestError` — the production diagnostic boundary's
(`CachedGhApi`/`DiagnosticTransport`) typed wrapper covering both a raw
transport exception and a permanent API-level rejection (e.g. an HTTP 500) —
as eligible to preserve already-retrieved records; only its
`TRANSPORT_FAILURE` classification (and a bare `httpx` transport exception)
is actually retried; every other classification is not worth retrying but
still raises `PartialPRChangedFilesError` with whatever was already fetched
rather than discarding it. An exception unrelated to the GitHub request
boundary (a genuine bug, not a retrieval failure) still propagates untouched
rather than being downgraded to a partial recovery. The per-file REST patch
completeness check
(`_github_file_record_has_complete_patch`) no longer excludes content lines
that happen to start with `++`/`--` (e.g. `++counter;`): GitHub's per-file
`patch` field never carries unified-diff `+++`/`---` file headers, only hunk
headers and content, so that exclusion was misclassifying complete evidence
as unavailable. The production orchestration regressions
`test_controller_recovers_exact_paths_in_one_same_session_round`,
`test_completion_rejects_response_from_a_rotated_backend_session`,
`test_completion_round_preserves_prior_recovered_path`,
`test_completion_transitions_unavailable_path_to_recovered_with_new_finding`,
`test_bypass_cache_uses_genuinely_cache_bypassing_retrieval`,
`test_unresolvable_file_count_reaches_completion_but_blocks_final_pass`,
`test_stale_raw_diff_for_an_already_decided_file_is_rejected_at_finalization`,
`test_unresolvable_file_count_is_retained_as_diagnostic_alongside_a_demonstrated_finding`,
`test_irrelevance_with_negated_marker_in_an_earlier_clause_is_still_accepted`,
and `test_partial_changed_file_page_failure_retains_successfully_retrieved_evidence`
(all in `tests/test_adversarial_validator.py`), plus
`tests/test_gh_cache_pr_changed_files_pagination.py` (including its
production-typed-error and unrecognized-exception cases), prove these
boundaries; existing dashboard stage rendering remains generic.

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
| Decomposition-publication resumption (Issue #2026) | `tests/test_decomposition_validation_publication_resumption.py::test_decomposition_publication_stage_handler_resumes_after_restart` (drives the new `_DecompositionPublicationStageHandler`, registered on its own `DECOMPOSITION_PUBLICATION_STAGE`, through the same `origin="decomposition-publication-resumption"` trace scope as the specification handler; asserts the durable per-effect completion contract). Before Issue #2026 the decomposition (parent-set) BLOCKED route did not participate in the pending-work store at all, so an interrupted findings comment or readiness withdrawal for a parent submission had no resumption path or trace emission; this closes that gap without changing the specification-route emission. |
| PR pending-work resumption | `tests/test_pr_production_instrumentation.py::TestPrResumptionSupersededHead::test_pending_work_resumption_records_superseded_on_changed_head`; `tests/test_dashboard_observability.py::TestJoinedProductionToView::test_pr_pending_work_resumption_reaches_detail_view_as_superseded` |
| Merge-operation resumption | `tests/test_pr_production_instrumentation.py::TestMergeOperationResumeSupersededHead::test_merge_operation_resumption_records_superseded_on_changed_head`; `tests/test_dashboard_observability.py::TestJoinedProductionToView::test_merge_operation_resumption_reaches_detail_view_as_superseded` |
| Asynchronous PR adversarial validation | `tests/test_dashboard_observability.py::TestNewOriginCoverage::test_asynchronous_pr_adversarial_validation_origin_is_recorded` (drives `_handle_pr_merge` through the real `AdversarialValidationScheduler` admission and reads the real `pr.adversarial-validation` event back; `test_take_pr_actions_preserves_structured_adversarial_failure` mocks `_handle_pr_merge` itself, so it does not exercise this emission) |
| Codex Cloud failed-correction redelivery generation | `tests/test_adversarial_validation_pr_flow.py::TestAdversarialValidationCodexFeedback::test_codex_completed_turn_redelivers_failed_correction_once_after_restart`, `test_rejected_failed_correction_retains_first_observed_generation_when_later_turn_appears`, `test_codex_batch_keeps_each_findings_own_remediation_baseline`, `test_initial_provider_receipt_recovery_does_not_satisfy_later_generation`, and `test_accepted_validation_snapshot_survives_initial_thread_lookup_failure`, plus `tests/test_codex_wham_client.py::TestCodexWhamClient::test_provider_newest_first_history_uses_timestamp_order_for_both_selectors` (use the production routing/client and provider-shaped WHAM boundaries to verify validation association, chronological ordering, immutable selected generations, per-finding baselines, crash-window receipt recovery, failed routing, and duplicate suppression). The route retains the existing `Cloud Task Adversarial Feedback` event schema and emits it only after confirmed follow-up delivery. |
| Material test-oracle gap reconciliation | `tests/test_adversarial_test_oracle_gaps.py::test_same_head_addressed_gap_thread_persists_before_unrelated_inconclusive_resolution` (covers omitted and explicit-OPEN response variants and reopens the durable reviewer store before the external thread-resolution boundary). This changes the reconciled validation outcome but introduces no processing origin or structured event field; the existing `pr.adversarial-validation` result emission and dashboard consumer remain authoritative. |
| Adversarial gap-state acceptance fence | `tests/test_adversarial_validation_pr_flow.py::TestAdversarialValidationPRFlow::test_forced_only_retry_runs_for_an_already_validated_head` (registers a newer attempt before the owning serialized application boundary and proves the stale reviewer checkpoint is not saved or published). Head-unavailable and persistence-failure outcomes retain the existing `pr.adversarial-validation` event schema and are distinguished by the recorded application phase and validation diagnostic. |
| PR adversarial-validation backend exhaustion (`EXHAUSTED`) | `tests/test_adversarial_validation_config.py::TestResolveAdversarialValidationAvailabilityExhaustion` (candidate-route classification: whole-set exhaustion, disabled/incapable exclusion, non-quota unavailability, quota-unknown/runnable fallback, missing-reset-time cooldown); `tests/test_adversarial_validator.py::TestRunAdversarialValidation::test_run_adversarial_validation_reports_exhaustion_instead_of_blocked` (fresh selection surfaces `EXHAUSTED` instead of the generic `BLOCKED`); `tests/test_adversarial_validation_pr_flow.py::TestAdversarialValidationPRFlow::test_fresh_exhaustion_publishes_exhausted_and_does_not_merge`, `test_exhausted_same_sha_not_due_skips_revalidation`, `test_exhausted_same_sha_due_triggers_automatic_retry_without_force`, and `test_exhausted_retry_superseded_by_newer_attempt_performs_no_publication` (drive `_handle_pr_merge` end to end: publication, fail-closed non-merge, same-HEAD dedup while not due, the automatic due retry without `--force`, and supersession of a pending retry by a newer same-HEAD attempt). `EXHAUSTED` reuses the existing `pr.adversarial-validation` event schema; the new distinguishing signal is `Outcome.DEFERRED` (instead of `Outcome.BLOCKED`) plus a `retry_not_before_epoch` metadata field, and the retry-not-before time itself is carried durably in the published comment/review body rather than in a new local store, so it survives restart without a bespoke schema addition. |
| Forced same-head admission past the unresolved-thread gate (issue #2106) | `tests/test_adversarial_validation_pr_flow.py::TestClaimedReviewThreadValidationFlow::test_forced_revalidation_reaches_validation_despite_same_head_error_and_unresolved_thread`, `test_forced_revalidation_with_mixed_threads_only_promotes_authentic_root`, `test_forced_revalidation_bypasses_post_codex_recheck_blocker`, and `test_non_forced_run_with_same_head_error_and_unresolved_thread_does_not_start_validation` (drive `_handle_pr_merge` end to end through the initial unresolved-thread gate and the post-Codex-review recheck). Both existing `pr.review-thread-gate` and `pr.repair-delegation` events keep their schema; the new admission path is distinguished by a `Continuing to forced adversarial validation` / `Forcing adversarial validation ... via explicit --force` action rather than a new field. The forced attempt still reuses the unchanged `pr.adversarial-validation` event for its own result. |
| Fresh review-thread read at the merge boundary (issue #2106) | Same tests as above, plus `test_forced_revalidation_with_mixed_threads_only_promotes_authentic_root` (asserts `merge_pr` is not called and the human thread remains unresolved). This adds one more `pr.review-thread-gate` emission (`phase: "merge-boundary"` in its metadata) immediately before merge, reusing the existing event name/schema; it fires for every merge attempt (forced or not) whenever the review-thread gate is enabled, not only the forced path. |
| PR adversarial-review durable audit (issue #1985) | `tests/test_pr_adversarial_review_audit.py` (drives `_handle_pr_merge` and the real `BackendManager`/`run_adversarial_validation` boundary for one-call PASS/ERROR execution, post-feature and legacy reuse, local-only-blocked and disabled-bypassed non-executions, backend-fallback and dynamic-follow-up multi-call reviews, and a paired instrumented/instrumentation-disabled comparison including an injected audit-write failure). This is observability-neutral for the existing `pr.adversarial-validation` execution-trace event and `TraceCollector`/dashboard-detail schema covered above: it adds a separate, independently-queryable `ReviewAuditStore` (`review_kind="pr_adversarial"`) recording the same boundaries this table already covers (admission/reuse/bypass, execution, publication/reconciliation), but introduces no new processing origin, admission gate, outcome value, provider-routing branch, or structured `_record_pr_stage`/`TraceCollector` field, and its own recording never runs in a path that decides eligibility, merge safety, or review/publication policy. |
| Issue specification/decomposition review durable audit (issue #1984) | `tests/test_issue_specification_decomposition_review_audit.py` (drives `AutomationEngine._traced_validation_job`/`_schedule_parent_validations` and the real `SpecificationValidationLifecycle`/`DecompositionValidationLifecycle`/`BackendManager` boundary for a fresh EXECUTED READY round-trip readable after a fresh-collector restart, REUSED reuse-provenance linking plus a legacy-decision-with-no-association fallback, malformed-backend-output/local-Objective-integrity-refusal/disabled-BYPASSED as distinct outcomes, a parent+open-child+closed-child decomposition identity, two scheduler waiters coalescing onto exactly one review, and an unwritable audit store that never changes the returned decision). Mirrors the PR adversarial audit above: it adds a separate, independently-queryable `ReviewAuditStore` (`review_kind` `"issue_specification"`/`"issue_decomposition"`) recording the same "Individual/decomposition validation jobs" boundary this table already covers, but introduces no new processing origin, admission gate, outcome value, provider-routing branch, or structured `TraceCollector` field, and its own recording never runs in a path that decides Issue readiness, remediation, or publication. |
| PR repair exhaustion and operator resumption (issue #2142) | `tests/test_pr_repair_exhaustion.py` (covers end-to-end non-convergence stopping dispatch across all repair origins, fail-closed non-merge, canonical blocker hold clearing upon human revalidation, atomic operator grants and re-evaluation scheduling via CLI `auto-coder pr-repair resume`, crash recovery of unfulfilled grant re-evaluations, and review limits never turning into repair success). When repair allowance is exhausted, automatic merge and repair delegations are blocked with `Outcome.BLOCKED` and metadata containing `{"reason": "repair allowance exhausted", "blocker_ids": list(...), "machine_readable_reason": "AUTO_REPAIR_EXHAUSTED"}`; explicit operator resumption records a trace under `pr.repair-resumption` before scheduling re-evaluation in `PendingWorkStore`. |

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

The CLI preserves a diagnostic-bearing `deferred` result before explicit target
resolution, without changing production traces or asserting a resolved target type.
This presentation correction is observability-neutral: origins, admission, provider
routing, and event schemas are unchanged.
`tests/test_process_issues_cloud_only.py::test_process_issues_only_completion_status_uses_target_outcome`
verifies preserved deferrals, missing diagnostics, and target-number mismatches.

Issue relationship reads use the shared HTTP cache and emit request outcomes
under `controller-issue-read`. Fresh hits have local-cache provenance and do not
reserve a governor slot. Expired responses revalidate; local mutations invalidate
previous snapshots and relationship pages before readback.
`tests/test_issue_relationship_http_cache.py::test_explicit_preflight_and_repeated_family_checks_send_each_resource_once`
checks real cache transport request counts across repeated engine checks, and
`test_issue_cache_hit_records_local_provenance_without_admission` verifies wire
admission and local provenance. Run `bash scripts/test.sh tests/test_issue_relationship_http_cache.py tests/test_dashboard_observability.py`.

Governor admission aggregates its retained reservation history inside SQLite,
returning one row instead of materializing the whole hour in Python while holding
the shared write lock. This is trace-neutral: admission decisions, retry deadlines,
request outcomes, and their production emission points remain unchanged; it changes
only the cost of computing those same decisions. No new dashboard stage is implied.
`tests/test_github_request_governor.py::test_admission_materializes_bounded_rows_with_retained_history`
covers bounded result allocation for 100 and 10,000 retained requests;
`test_aggregated_admission_rejects_invalid_live_timestamps` preserves fail-closed
handling of corrupted evidence. Run `bash scripts/test.sh tests/test_github_request_governor.py`.

Explicit startup passes its completed relationship preflight into child-generation
validation. This removes a duplicate internal discovery pass; the original
`issue.explicit-relationship-discovery` event and all downstream admission events
retain their meaning. Coverage:
`tests/test_parent_issue_reconciliation.py::test_child_generation_reuses_completed_explicit_relationship_preflight`.

Automatic specification-repair budget exhaustion remains represented by the ordinary
category-specific BLOCKED production outcome and effects; it no longer fabricates the
terminal `reissue_required` admission outcome. The diagnostic explicitly identifies
the durable episode pause, while existing validation event kinds and dashboard outcome
projection remain unchanged because the semantic verdict is still BLOCKED. Run the
production lifecycle regressions with
`bash scripts/test.sh tests/test_specification_validation_lifecycle.py tests/test_decomposition_validation_lifecycle.py`.
Publication-only BLOCKED processing leaves the repair count unchanged; explicit
repair authorization persists its count before its initiator runs. Every normal-worker
and stale-session production call site that applies a freshly reviewed standalone,
inherited-child, or decomposition BLOCKED decision now performs that authorization
immediately before publishing the decision's diagnostic/readiness effects; durable
recovery of an already-decided pending publication (`_ValidationPublicationStageHandler`)
continues to republish those same effects without re-authorizing, so a replay never
double-counts. Complete-family individual blocking, unconditional durable-reissue
refusal during category disablement, and closed-parent effect refusal all return
through the existing Issue blocked/skipped result instrumentation, so no event schema
or dashboard projection change is needed.

# Review adjudication snapshots

The GitHub adjudication boundary exposes a read-only snapshot containing the
registered context, raw finding, contributing Issue references, actual actor and
source-comment identities, effective graph result and tips, and observation
revision. Context publication is an output of reconciliation, not an operator
decision or PASS signal. `SOURCE_UNAVAILABLE` suspends positive applicability
while retained history remains visible; no new trace event schema or dashboard
admission/outcome mapping is introduced by this boundary.

The boundary is invoked inside the existing PR-processing execution scope, so
its configured read or persistence failure is reported through the existing PR
error outcome rather than a new event kind. Successful refreshes are available
through the engine's read-only adjudication snapshot accessor; publication does
not emit PASS, repair, or review-resolution events. Issue-originated reverse
invalidation and startup recovery use the existing durable invalidation worker
origin and therefore preserve the production-to-view routing contract.

# Review adjudication effect orchestration

Applying an authorized adjudication decision's owned effect (Issue #2019) is a
new production origin distinct from the read-only snapshot boundary above: it
introduces a new `pr.review-adjudication-effect` stage, recorded once per
applied or attempted `UPHOLD`/`OVERRULE`/`REOPEN` effect via the existing
`_record_pr_stage` scoped-event helper, alongside the existing merge-gate
stages for the same PR-processing pass. Its outcome mapping reuses the
existing `Outcome` enum rather than inventing new terminal states: `delivered`
/`retired`/`reconciled` map to `COMPLETED`, `pending` to `DEFERRED`, `unknown`
to `UNKNOWN`, and a failed overrule reversal (`reconciliation-required`) to
`BLOCKED`. Facts carry the context/decision identity, effect status, and gap
identity when applicable, so a dashboard consumer can distinguish a confirmed
repair delivery from a still-pending one without conflating it with PR
approval or implementation verification (REQ-010).

This is new provider routing reuse, not a new provider: `UPHOLD` delivery
resolves the PR's existing durable cloud-task association exactly the way
ordinary unresolved-review-thread repair delegation already does
(`_resolve_cloud_task_origin`), so the existing provider-admission and
follow-up-support diagnostics remain the sole source of truth for whether a
repair route exists; this stage only reports what was attempted with it.
`OVERRULE`/`REOPEN` reuse the existing GitHub reply/resolve/unresolve
mutations already instrumented via `PRActionList` and (for resolve) the
existing thread gate, adding a distinct auditable marker
(`auto-coder-review-adjudication-overruled:v1`) rather than a new mutation
kind.

An adjudication-forced revalidation bypasses the ordinary same-head
adversarial-validation suppression the same way an explicit `--force` run
already does (`forced_same_head_revalidation`); it does not add a new
suppression-bypass event, since the existing forced-revalidation action
messages already cover both origins.

`tests/test_review_adjudication_orchestrator.py` covers the effect-planning
decision logic (idempotent same-generation suppression, shared test-oracle-gap
ownership, reopen-on-supersession) against real ledger state built the same
way the #2018 GitHub boundary itself builds it.
`tests/test_pr_processor_adjudication_effects.py` drives the full
read -> plan -> deliver/retire -> journal path against a fake GitHub client
and the real cloud-task-origin resolution path (mocking only the external
`CloudManager`/`CodexCloudClient` boundary, the same seam
`tests/test_codex_cloud_pr_review_flow.py` already uses), and confirms a raw
adjudication envelope reply is excluded from the generic cloud
review-feedback path it would otherwise be forwarded through verbatim. Run
`bash scripts/test.sh tests/test_review_adjudication_orchestrator.py tests/test_pr_processor_adjudication_effects.py`.

# Per-invocation shutdown-protection wiring

Issue #2009 wires the standalone `InvocationAdmissionGate`/`InvocationHandle`
model (Issue #2008, `invocation_admission.py`) into real production LLM
invocation boundaries: the shared `BackendManager._execute_backend_with_providers`
call (covering `run_llm_prompt`/`run_llm_noedit_prompt`/`run_prompt`, explicit
and automatic session continuation, and every backend/provider rotation
retry), the `SpecificationValidationLifecycle`/`DecompositionValidationLifecycle`
decision checkpoints, the Jules recurrent-task remote-handoff receipt, and the
`AutomationEngine` daemon lifetime (one gate per lifetime, installed alongside
the existing `install_admission_check` in `_run_local_critical`, closed on
`request_graceful_shutdown`, forced on `request_force_stop`).

This is an admission-gate and durable-resumption change, not a new processing
origin, outcome, provider route, or structured event. During ordinary RUNNING
operation the gate always admits, so every existing `_record_dispatch_stage`/
`TraceCollector` stage, origin, outcome, and provider-routing decision is
produced exactly as before (Issue #2009's REQ-010). The gate only ever refuses
admission while the daemon is already DRAINING/STOPPED/FORCED — the same
graceful-shutdown window `docs/client-features/graceful-daemon-shutdown.md`
already documents — and that refusal surfaces through the same pre-existing
`new_work_allowed()`-guarded "Deferred ... graceful shutdown is draining"
action text and `AutoCoderRetryableBackendError` paths those call sites already
had; it does not add a new dashboard-visible outcome value or event kind.
`SpecificationDecision`/`DecompositionDecision.evaluation_source` and their
existing persisted-decision schema are unchanged: this only adds a checkpoint
around the already-existing `store.save()` write, deferring invocation
settlement until that write is confirmed (or leaving it visibly unsettled and
retriable on a write failure), never altering what gets persisted or how a
dashboard/reuse consumer reads it. `gate.snapshot()`/`AutomationEngine.
invocation_admission_snapshot()` is new, purely diagnostic, process-internal
state (unsettled invocation identity/stage/lifecycle-state/checkpoint-failure
count) with no prompt/response/credential content; it is not wired into any
dashboard panel or `TraceCollector` event.

`tests/test_invocation_admission_wiring.py` drives the real production
boundaries end to end with a fake CLI client at the outermost provider-
transport seam: per-attempt admission across backend rotation, a deferred
checkpoint that stays protected until the caller's own durable write confirms
it, a checkpoint write failure that leaves the invocation unsettled and
retriable without a second paid call, admission refusal while draining (no
provider call at all), the Jules remote-handoff receipt settling the
invocation without waiting on the remote task, and the `AutomationEngine`
gate's installation/close/force lifecycle. Run
`bash scripts/test.sh tests/test_invocation_admission_wiring.py tests/test_invocation_admission.py tests/test_specification_validation_lifecycle.py tests/test_decomposition_validation_lifecycle.py tests/test_jules_engine.py`.

# Repository-scoped internal job trace interface

Issue #2000 (child A of the #1999 dependency-rescan observability tracking
parent) adds `src/auto_coder/repo_job_trace.py`, a new
producer/snapshot-consumer diagnostic interface for repository-scoped
internal jobs (currently `dependency-rescan`), identified by
`RepoJobTarget(repository, job_kind)` rather than an Issue/PR number. This
is observability-neutral for every existing processing origin, admission
gate, outcome, provider route, durable resumption path, and structured
event schema: `execution_trace.py`'s schema-version-1 `StructuredEvent`/
`TraceCollector`, `dashboard_detail.py`, and `entity_invalidation.py`'s
`dependency:1` durable token/generation/lifecycle are untouched, and no
production code path calls the new module yet -- it has no producer wired
into `entity_invalidation.py`'s dependency fan-out (child B, #2001) and no
dashboard route (child C, #2002). `tests/test_repo_job_trace.py` is a
model-level regression suite for this new interface's own contract
(target isolation from the Issue/PR namespace including the "Dependency #1"
sentinel-collision case, fresh execution identity per retry/recovered
attempt, explicit-reference-only correlation, bounded/truthful snapshot
retention and clipping, restart-safe absence of fabricated history, and
diagnostic-failure/business-outcome independence); it intentionally does
not claim a production-to-view regression, which is owned by #2001/#2002
once a real producer and dashboard route exist. Run
`bash scripts/test.sh tests/test_repo_job_trace.py tests/test_execution_trace.py tests/test_dashboard_detail_logic.py`
for this boundary.

Issue #2001 (child B of the #1999 dependency-rescan observability tracking
parent) wires a real producer into `repo_job_trace.py`'s
`(repository, "dependency-rescan")` target: `AutomationEngine.
_expand_dependency_obligation` now opens its own `RepoJobExecutionScope`
around authoritative enumeration (`GitHubClient.get_open_entities_strict`)
and every per-Issue handoff, `AutomationEngine.invalidate_entity` records
dependency-triggering webhook intake (event/action/delivery/source-Issue
evidence) and, when called from inside that scan's scope, each Issue
handoff's actual committed disposition, and `AutomationEngine._worker_loop`
records the dependency job's own durable-claim acknowledgement as a late
stage-reached fact against the same execution id once the outer claim
completion/release actually happens. `DurableInvalidationQueue.invalidate`/
`complete`/`recover` gained `invalidate_with_transition`/
`complete_with_outcome`/an enriched `recover` return value that observe the
real committed transition (`new_pending`/`coalesced`/`followup_required`,
`cleared`/`followup_pending`/`stale_no_op`, and the actual recovered
identities) at the same locked state-owning boundary that performs it; the
existing `invalidate()`/`complete()` Boolean return and every other
durable-queue behavior (webhook acceptance/rejection, coalescing,
stabilization deadlines, retry/claim transitions, CI/PR paths) are
unchanged -- these are thin wrappers over the richer calls, and no existing
caller consumed `recover()`'s previous `None` return. This still adds no new
processing origin, admission gate, outcome, or provider route for Issue/PR
processing itself (`execution_trace.py`'s schema-version-1 interface,
`dashboard.py`'s `active_workers`/queue status projection, and every
downstream Issue/PR execution scope are untouched and continue exactly as
before); it is purely additive diagnostic evidence for the repository-scoped
scan that surrounds them, and a diagnostic-recorder failure at any of these
boundaries is caught and logged without changing the real webhook response,
durable transition, or scan/handoff outcome it describes (REQ-009). A
dashboard route for this evidence remains child C (#2002)'s scope.
`tests/test_entity_invalidation.py` adds direct `DurableInvalidationQueue`
coverage for the three richer transition/outcome/recovery APIs, and
`tests/test_dependency_rescan_repo_job_trace.py` is the production-path
regression suite: it drives real `/hooks/github` deliveries through
`create_app`, the real durable queue, and the real worker loop, then reads
back `RepoJobTraceCollector`'s snapshot to verify intake evidence (accepted,
duplicate, and persistence-failure), a running scan visible mid-enumeration/
mid-handoff, discovered-Issue-count and per-disposition handoff totals
derived from the recorded transitions (not a legacy Boolean or queue
length), an incomplete scan on enumeration/handoff failure, recovered
pending work after a restart, and that diagnostic-recorder failure changes
none of the real business outcome. Run
`bash scripts/test.sh tests/test_repo_job_trace.py tests/test_entity_invalidation.py tests/test_dependency_rescan_repo_job_trace.py`
for this boundary.

# Shutdown wait narrowed to protected LLM invocations

Issue #2010 changes what `AutomationEngine.start_automation`'s graceful
shutdown branch actually waits on before reporting the daemon STOPPED, and
narrows what the existing "Draining local critical operation(s)"/"Waiting for
N local critical operation(s)" diagnostics may call critical: this is an
admission-gate/durable-resumption change to *when the daemon exits*, not a
new processing origin, outcome, provider route, or structured event, so
`execution_trace.py`'s schema, `TraceCollector` stages, and every existing
dispatch/outcome/provider-routing record continue exactly as before.

`AutomationEngine._wait_for_protected_invocations` (new) is awaited first and
blocks only on `self.invocation_gate.snapshot()` reaching graceful readiness
-- the same `InvocationAdmissionGate` Issue #2009 already wired into every
real LLM invocation boundary, now finally consulted for the daemon's own
exit. `_wait_for_interrupted_local_work` (the renamed former
`_wait_for_local_critical_operations`) still reaps `_critical_operations`
bookkeeping afterward, but is expected to settle quickly instead of blocking
on an unrelated operation's own completion, because two new mechanisms
interrupt that unrelated work as soon as `request_graceful_shutdown` closes
admission: the cooperative `new_work_allowed()` checks already threaded
through `issue_processor.py`/`pr_processor.py`/`validation_scheduler.py`, and
a new one in `utils.CommandExecutor`'s command-execution loop that kills any
locally owned subprocess that is not part of the one still-admitted
invocation's own controlled provider call or tool tree
(`shutdown_interrupt.mark_invocation_active`/`is_invocation_active`, set only
around the actual provider call inside `BackendManager
._execute_backend_with_providers`). `update_manager.maybe_run_auto_update`
now runs its upgrade command through `CommandExecutor.run_command` instead of
a bare `subprocess.run`, specifically so this interruption reaches the
concrete "update check" operation the reported regression named.

`request_graceful_shutdown`'s own diagnostics are split accordingly: a
"Waiting for N protected LLM invocation(s)" line names each unsettled
invocation's repository/target/stage/state from `gate.unsettled_snapshot()`,
and a separate "Interrupting N unrelated local operation(s)" line reports
`_critical_operations`'s coarser descriptions without calling them critical
or paid work. `AutomationEngine.get_status()`'s existing
`local_critical_operations` diagnostic field is unchanged (still the same
coarse thread-ownership descriptions, still process-internal-only, not
dashboard-routed); a new sibling field, `protected_invocations` (the same
repository/target/stage/state tuples), is added next to it for the same
purpose, with no prompt/response/credential content.

`tests/test_graceful_shutdown.py` adds regressions for: an unrelated
maintenance operation's real subprocess being killed rather than joined once
shutdown closes admission (mirroring the reported "update check" hang) while
a concurrently admitted invocation's own subprocess is left untouched; the
daemon reporting STOPPED once the protected gate drains even while an
interrupted non-LLM operation is still winding down; and `update_manager`'s
own `maybe_run_auto_update`/`check_for_updates_and_restart` regression suite
(`tests/test_update_manager.py`) is updated for the `CommandExecutor.
run_command` call it now makes. Run
`bash scripts/test.sh tests/test_graceful_shutdown.py tests/test_invocation_admission_wiring.py tests/test_invocation_admission.py tests/test_update_manager.py tests/test_utils.py`
for this boundary.
Issue #2102 adds the production merge-authorization projection for that lifecycle.
Its trace-facing diagnostics expose the selected phase/backend, audited and current
heads, waiting reason, outstanding stable finding IDs, and the direct-strong-pass or
bounded-ordinary-closure completion basis. These fields belong on the existing PR
review/action detail surfaces; the feature does not add a Dashboard page. A pending
strong audit, unconfirmed publication, open finding, active newer attempt, or stale
head/base/contract/policy identity must be rendered as waiting or blocked, never as
a successful review or merge authorization.

# Crash-safe Jules candidate submission (inactive adapter)

Issue #2071 adds a durable candidate submission/reconciliation adapter, but
does not connect it to public fan-out, singleton Jules dispatch, worker
admission, provider routing, or dashboard projection. Consequently there is
no new production processing origin, admission outcome, structured event, or
durable-resumption path visible to the dashboard in this stage; adding a
dashboard trace before the final integration would falsely imply that the
adapter is reachable. Existing `issue.dispatch.jules` events and their
dashboard coverage remain unchanged. The production-boundary tests in
`tests/test_jules_candidate_submission.py` establish that the inactive
adapter preserves its durable claims and exact reconciliation state without
substituting dashboard diagnostics for provider/session ownership.
# Explicit Issue-review rerun authority

Explicit review reruns add durable authorization state but do not add a new
structured dashboard event or change an existing event schema. The existing
individual/decomposition validation-job traces continue to describe actual
review executions; request inspection uses the durable rerun operation status
(`pending`, `deferred`, `satisfied`, or `superseded`) and its decision
reference/source. This is intentionally observability-neutral for the trace
pipeline: reset acceptance, cache cleanup, and queue admission are not review
outcomes and must not be emitted as successful validation jobs. Runnable
authority and stale-completion coverage lives in
`tests/test_issue_review_rerun.py`, while Review-lane coalescing coverage
remains in `tests/test_issue_review_worker.py`.
