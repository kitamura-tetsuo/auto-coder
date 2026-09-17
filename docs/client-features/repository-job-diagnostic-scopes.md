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

This module is intentionally scope-limited: it does not instrument the
production webhook/worker paths (see the child issue that wires
`entity_invalidation.py`'s dependency fan-out into it) and it adds no
dashboard route (see the child issue that exposes it as a read-only detail
view). It also does not change the durable queue's `dependency:1` token,
invalidation generation, or lifecycle in `entity_invalidation.py`,
consistent with `docs/dashboard-observability.md`'s existing distinction
between the durable queue's coalescing generation and a diagnostic
execution identity. `tests/test_repo_job_trace.py` covers target
resolution/isolation (including the Issue/PR-#1 sentinel-collision case),
generation-reuse across retries and interleaved asyncio/thread attempts,
snapshot immutability and truthful clipping/eviction, restart-safe absence
of fabricated history, diagnostic-failure/business-outcome independence,
and non-interference with the existing Issue/PR recorder and its legacy/
unsupported-record handling. Run `bash scripts/test.sh
tests/test_repo_job_trace.py tests/test_execution_trace.py
tests/test_dashboard_detail_logic.py` for this boundary.
