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

Preparation fixtures retain a bounded JSONL observation in the mounted CI timing
directory, which the integration job uploads with its existing diagnostics.
Records label actual cold, warm, uncached, or fallback setup and
allowlist uv's downloaded, built, and prepared summaries without retaining raw
URLs, credentials, or arbitrary installer output. A completed preparation marker
identifies which invocation populated the cache for a later warm invocation. The
independent same-source staged-runtime case owns a freshly reset comparison
namespace: its first installation is the identified cold populator and its second
fresh destination is the warm sample, with no intervening writer. A retained
comparison event joins exact source, lock, Python, uv, namespace, destination,
population-lineage fields, and a per-execution correlation identifier shared only
by that preparation's uv observation and completed timing spans. This prevents
retained spans from an earlier run with the same human-readable invocation label
from completing a newer partial measurement. The comparison reports reusable downloads/builds separately
from the required `auto-coder` source build.
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
minimum elapsed-time or speedup claim. In Supervisor Integration run
`42031185285` for source `b17b6ffcb5d3cd481202cf9f26ccf7ac15e55a6d` and lock
digest `4207d1ee5e471481cfc27a6abf5d8124d0f865dbb1151436f040505449d12764`,
the retained real nested-Docker comparison recorded 26 completed reusable
downloads for the cold fresh environment and zero for the warm fresh environment.
Pinned uv 0.7.22 still prepared and installed each independent environment and
rebuilt `auto-coder` from its checkout in both samples. This is evidence for those
exact inputs, not a universal latency or speedup claim.
