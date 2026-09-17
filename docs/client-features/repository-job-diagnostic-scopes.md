# Repository-job diagnostic scopes

`src/auto_coder/repo_job_trace.py` gives repository-wide internal jobs (for
now, the dependency-rescan fan-out) their own producer/snapshot-consumer
diagnostic interface, entirely separate from the schema-version-1 Issue/PR
interface in `execution_trace.py`. A job is identified by a
`RepoJobTarget(repository, job_kind)` built only through
`resolve_repo_job_target`, which rejects an empty repository or any
`job_kind` other than the supported `RepoJobKind` values (currently
`dependency-rescan`) instead of guessing a target -- there is no synthetic
GitHub number and no fallback to "the repository's most recent record".
This directly replaces the earlier defect where the dashboard linked the
internal `dependency:1` queue token to an Issue/PR detail route and
displayed a nonexistent "Dependency #1".

`RepoJobTraceCollector` (singleton via `get_repo_job_trace_collector()`) is
storage-independent from `execution_trace.TraceCollector`: a job observation
can never be filtered into, or mistaken for, Issue/PR evidence, and vice
versa. Repository-scoped `record_intake`/`record_queued`/`record_recovered`
observations never carry an execution identity and never manufacture a scan
attempt or completion by themselves. `start_execution` always mints a fresh
opaque execution id -- a retry or a recovered attempt never reuses an
earlier attempt's identity merely because the repository, durable queue
token, invalidation generation, source webhook delivery, or inputs match.
Nested `record_stage_reached` observations propagate that identity via
`contextvars`, with `bind_repo_job_scope`/`current_repo_job_scope` for
explicit thread/task hand-off, mirroring `execution_trace.bind_scope`.
Correlating an earlier unscoped observation with a later execution is only
ever done through an explicit producer-supplied
`RepoJobFacts.source_observation_refs` reference to that observation's own
`observation_id`; nothing in this interface attaches evidence by matching
generation, timing, or "the most recently active worker".

`RepoJobFacts` is a typed, frozen dataclass (not a free-form mapping), so a
job's recorded facts are structurally limited to references, counts,
phases, and availability -- there is no field that could carry a raw
request/response body, a credential, or raw Issue content. Its
`source_issue_refs`/`target_issue_refs`/`trigger_delivery_refs` fields are
`ClippedNumberRefs`/`ClippedTextRefs`: a bounded, immutable tuple plus an
independent `total_count` that a producer can supply as an exact aggregate
even while the retained list itself is clipped (an unknown total is never
coerced to zero or to the clipped list's length). A target/source Issue
reference documents a handoff, not proof of that Issue's execution,
eligibility, or implementation. Both observation and execution-metadata
retention are bounded (`RepoJobTraceCollector(max_observations=...,
max_executions=..., max_refs_per_record=...)`), and exhausting either bound
sets `RepoJobSnapshot.observations_truncated` /
`execution_metadata_truncated` rather than silently dropping evidence.
Trace-sink failures are caught and logged via loguru and never change a
business return value/exception, and this module makes no GitHub/provider
or queue call itself.

`tests/test_repo_job_trace.py` covers target resolution/isolation (including
the Issue/PR-#1 sentinel-collision case), generation-reuse across retries and
interleaved asyncio/thread attempts, snapshot immutability and truthful
clipping/eviction, restart-safe absence of fabricated history,
diagnostic-failure/business-outcome independence, and non-interference with
the existing Issue/PR recorder and its legacy/unsupported-record handling.
Run `bash scripts/test.sh tests/test_repo_job_trace.py
tests/test_execution_trace.py tests/test_dashboard_detail_logic.py` for this
boundary.

Issue #2001 wires a real producer into this interface. `AutomationEngine.
_expand_dependency_obligation` opens one `RepoJobExecutionScope` per actual
scan attempt (never one merely for durable intake/queueing) covering
authoritative Issue enumeration and every per-Issue handoff;
`AutomationEngine.invalidate_entity` records the webhook-triggered intake
evidence for the `dependency` durable identity (event/action/delivery/
source-Issue references and the actual accepted/duplicate/failed outcome)
and, only when called from inside that scan's own scope, each Issue
handoff's actually committed disposition; and `AutomationEngine._worker_loop`
attaches the scan's own durable-claim acknowledgement as a late
stage-reached fact on the same execution id once the outer claim
completion/release genuinely happens. `DurableInvalidationQueue` gained
`invalidate_with_transition`/`complete_with_outcome`, plus an enriched
`recover` return value, that observe the real committed transition at the
same locked boundary that performs it, instead of guessing from the legacy
Boolean; `invalidate()`/`complete()` keep their exact prior Boolean
semantics as thin wrappers, and every durable-queue business behavior
(webhook acceptance/rejection, coalescing, stabilization, retry/claim
transitions, CI/PR handling) is unchanged. It still does not add a dashboard
route (see the child issue, #2002, that exposes this evidence as a read-only
detail view), and it does not change the durable queue's `dependency:1`
token, invalidation generation, or lifecycle, consistent with
`docs/dashboard-observability.md`'s existing distinction between the durable
queue's coalescing generation and a diagnostic execution identity.
`tests/test_dependency_rescan_repo_job_trace.py` is the production-path
regression suite for this wiring: real `/hooks/github` deliveries through
`create_app`, the real durable queue, and the real worker loop, reading the
resulting evidence back from `RepoJobTraceCollector`'s snapshot. Run
`bash scripts/test.sh tests/test_repo_job_trace.py
tests/test_entity_invalidation.py
tests/test_dependency_rescan_repo_job_trace.py` for this boundary.
