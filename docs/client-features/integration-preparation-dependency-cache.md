# Integration preparation dependency cache

The isolated Supervisor integration job assigns `AUTO_CODER_INTEGRATION_UV_CACHE_DIR`
to a runner-owned directory. Only disposable containers that execute the real
`WorkerVersionPreparer` receive that directory as `/dependency-cache/uv` and an
explicit `UV_CACHE_DIR`; worker and management runtime containers do not receive
the mount. An unset selector keeps the ordinary uncached path. If the harness
cannot create or use the selected directory before launch, it records a fallback
and launches the same preparation without a cache mount.

The cache contains uv-managed downloaded and built artifacts, never checkouts,
virtual environments, runtime identifiers, READY receipts, application state, or
test results. Every preparation therefore retains a fresh destination and the
existing frozen, non-editable install and installed-provenance verification.
Runtime and per-test cleanup remain separate from the job-owned cache lifetime.

Preparation fixtures retain a bounded JSONL observation beside existing runtime
diagnostics. Records label actual cold, warm, uncached, or fallback setup and
allowlist uv's downloaded, built, and prepared summaries without retaining raw
URLs, credentials, or arbitrary installer output. A completed preparation marker
identifies which invocation populated the cache for a later warm invocation.
Unrecognized output is reported as unavailable rather than inferred from elapsed
time, and diagnostic failures never replace the installer result.

To reproduce the integration selection, prepare the production image, set
`AUTOCODER_SUPERVISOR_IMAGE`, `AUTO_CODER_REQUIRE_SUPERVISOR_INTEGRATION=1`, and
point `AUTO_CODER_INTEGRATION_UV_CACHE_DIR` at an empty writable directory. Run the
same selected container tests twice (or let their multiple fresh preparations use
the shared directory), retaining fresh runtime destinations. Compare identical
source, lock, Python, and uv inputs and the observed reusable dependency work;
phase duration is contextual evidence only. Source-package builds and real
lifecycle or heartbeat waits remain required work, so this optimization makes no
minimum speedup claim.
