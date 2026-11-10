# Tested source references

Every successful `Test Source for Runtime` run on `main` can publish one immutable
annotated tag named `auto-coder-tested-<full-source-SHA>`. This workflow is
independent of Docker image publication. It uses Python 3.12 and the locked
`uv.lock` input, checks Black and isort without rewriting, runs Flake8 and
package-wide mypy, launches a real headless Chromium, and executes the complete
pytest selection excluding only `opencode_live`.

Pull requests also run that same complete selection against the exact PR head;
the source-quality job checks GitHub's merge commit. PR runs provide validation
evidence only and cannot publish a tested-source reference. Publication remains
restricted to successful push runs on `main`.

The tag annotation is bounded canonical JSON with schema version, repository,
full source SHA, distribution version, lock hash, Python version, originating
workflow ID/run/attempt/path, the fixed `source-required-v1` profile, and the
successful result. A tag alone is not eligibility evidence: consumers must also
read the tag object and direct commit and verify the recorded Actions run and
both required jobs are completed successfully for that exact attempt and SHA.

Only the completion-triggered publication workflow has `contents: write`; test
jobs have read-only contents permission. The fixed workflow repository, CLI
destination, and checked-out GitHub `origin` must all identify the same
repository before a Git-data client is created. Publication is create-only.
Existing same-SHA tags are reused only after strict tag, annotation, commit, run,
and job readback. Conflicting data is never overwritten. If a create response is
lost, only exact readback can establish success; absent or unavailable evidence
remains uncertain.

To investigate a conflict or unavailable result, inspect the recorded Actions
run and attempt, both required job conclusions, `git/ref/tags/<name>`, and its
`git/tags/<object-id>` object through the GitHub API. Do not delete or force-move
the ref. Credentials and API response bodies are not placed in annotations or
logs. The legacy `Publish Beta` image publication path and historical registry tags
remain available for bootstrap and operator use. Both complete source-test paths
prepare the locked Python-only management fixture before their unchanged test
selection; this is not an OpenCode live-runtime preparation or image publication.
The fixture is exported only after immutable Docker ID and installed source/version
readback match the clean checkout. Missing Docker, failed build or mismatched
provenance blocks the gate rather than skipping its readonly-bind test.
The Supervisor fresh-host workflow independently verifies the image built
from its checkout, passes the inspected immutable ID to the retained-startup
consumer through the privileged `sudo env` boundary, and refuses a stale tag.
Manual execution using the historical tag similarly resolves that local tag to
its immutable ID and verifies installed checkout/version before any assertion
container starts. A tag alone cannot authorize the target test.
The retained-startup consumer re-inspects the exact immutable Docker ID
exported by the preparer and checks installed source revision/distribution
version against the checked-out source before executing its real Docker mount
and lock tests. The consumer forbids registry pulling (`--pull never`);
missing, removed, mutable or wrong-source images fail before any success
evidence can be emitted. The real-container regression also exercises the
nonempty-WAL and writable-journal refusal cases. An isolated deliberate
mutation removing the read-only mount guard makes this same real oracle reject
the resulting false-success, without altering the required workflow selection.
Fixture source SHA, immutable image ID and version are logged in the existing
preparation/test steps, while the complete-source shard receipt and normal
Actions/pytest logs record the actual selected and executed node outcomes.
A passing fixture check alone does not prove unrelated CI or worker health.
When the consumer refuses a dirty working tree, the error includes a bounded
read-only porcelain Git status excerpt to identify modified or untracked paths.
The excerpt is diagnostic only: the checkout remains rejected, and no image or
retained-startup assertion runs on a dirty source.
The complete-source Chromium installation and fresh-host downloaded plan
are located under the runner's temporary directory, not inside the source
worktree, so their generated files cannot falsely dirty the checkout.

Nonfatal scheduled pytest diagnostics use an owned Python thread and the synchronous
all-thread faulthandler API while holding the GIL. This avoids the demonstrated
native watchdog race with freed code metadata on the supported Python runtime.
The configured Source 60-second and ordinary-shard 30-second intervals, exit
behavior, complete selections, assertions, retries and hard deadlines stay intact;
workflow files and production code do not change. Native fatal-signal handling
and the original pytest descriptor/handler restoration remain enabled.

Each timer owns a duplicate output descriptor and is cancelled and joined before
completion, interactive exceptions or debugger entry can release it. Diagnostic
errors fail visibly. Explicit disabled/default-zero settings remain unchanged.
A Python thread can be delayed while native code holds the GIL; absence of a
timely dump never proves a pass, and the existing test/CI hard failure deadlines
still apply. Diagnostic output is neither Source qualification nor runtime
recovery proof. The actual main Source/publication runs and installed worker
verification remain separate requirements.

The configured `faulthandler_exit_on_timeout=true` path delegates to pytest's
original native timeout protocol, including prompt exit while native code holds
the GIL. Only nonfatal scheduled dumps use the Python-owned timer; this change
does not replace a hard deadline with a GIL-dependent Python thread. A real
`ctypes.PyDLL` regression holds the GIL past the deadline and checks that the
process exits before the native call completes.

## Current time-budgeted complete-source partition

The complete `not opencode_live` selection is collected on the actual source
checkout by `scripts/plan_ci_shards.py`, producing one immutable
`ci-shard-plan-v1` with ordered inventory, fingerprint, semantic digest,
disjoint contiguous assignments, a 420-second estimated work budget and at
most twelve nonempty shards (`max-parallel: 12`). New and parameterized nodes
receive labeled positive fallback estimates and cannot disappear; even an
indivisible 820-second case is not killed or skipped. Planning and execution
retain Python 3.12, locked test dependencies and required browser/fixture
inputs. Every shard prepares the verified Python-only management fixture,
installs and launches real headless Chromium, and retains native diagnostics.
The separate source-quality job retains all four Black/isort/Flake8/mypy
commands on the PR merge checkout; source tests use the exact PR head/main SHA.

The plan artifact is immutable and unique to workflow run and planning attempt.
Each shard downloads that exact plan, validates the full current post-selector
collection before selecting its assigned nodes and writes an invocation-owned
`source-ci-shard-receipt-v1`. The receipt contains the complete ordered
collection, assigned/selected/executed nodes, native setup/call/teardown
reports, native pytest exit status, timestamps, profile, plan and producing
run/attempt identity. A missing receipt or write failure blocks qualification.
Native failure or teardown error cannot become success; legitimate prerequisite
skips and xfails retain their native outcomes instead of being relabeled PASS.

`Complete source tests with Chromium` remains the publication-facing aggregate.
It fails unless complete paginated Actions jobs/artifacts contain the exact
planning job, one native completed-success job with all mandatory steps per
planned shard, and one matching immutable native receipt per shard. The gate
compares actual source checkout, ordered inventory and digest, profile, plan
digest, run, planning attempt, producing invocation and job timestamps. It
rejects stale or foreign plans, missing/duplicated/failed/mixed-plan results,
and a latest API alias representing a previously produced successful job under
a new attempt ID. Failed-only reruns cannot qualify if their plan or retained
shards were produced in an earlier attempt: rerun the complete source workflow
on one attempt. A failed current execution never borrows historical success.

Historical eligible unsharded and fixed-four-shard annotations still require
their originally recorded quality/aggregate Jobs and unchanged annotation
schema, workflow ID, exact SHA/lock/version; the new receipt gate applies only
to the current source-execution aggregate. No `Publish Beta`, OCI routing,
branch-protection or merge-authority change is made.

The workflow optionally restores a profile-scoped native timing snapshot from
immutable Actions cache, generates explicit per-shard JSONL batch manifests,
and saves new cache/artifacts under unique run/attempt names without tracked
file writes. The new snapshot is generated in a separate immutable output
directory, then staged into the exact `source-history` path consumed by the
planner and both cache restores before the cache save. Restore and save share
the same cache path/version and exact per-run/attempt key; a failed optional
update or staging step cannot publish an unchanged restored profile as a new
snapshot. Failed history restore, update or upload is diagnostic; missing
mandatory plan or receipt always fails. A `source-ci-metrics-v1` artifact
distinguishes queue, setup, test-step shard critical path, total runner work,
and end-to-end required-check latency (only when finished). It reconstructs
the previous four-way `duration_based_chunks` assignments for the **same new
inventory**, using the old checked-in duration snapshot, and compares them
using the newly observed native test phases. This counterfactual excludes
queue/setup and is not a separately measured old CI run. Ten minutes is an
observational target, not a test timeout or guarantee; a complete real-source
Actions execution is required before claiming an actual speedup.

### Historical four-way timing evidence (before this change)

The first PR execution of the partition (run `41763251437`, attempt 1, revision
`31857c14afd18598d2b149175f39d2e4e10f1dd4`) is recorded separately from the
last successful unsharded `main` execution (run `41757750713`, revision
`06e05ed4a43fe92eba91ee6f8fee689cf24f6e76`). These are adjacent observed
revisions, not identical-runtime benchmark inputs, so the comparison describes
the measured workflow behavior without claiming a controlled speedup or a
successful source qualification: shard 1 exposed an existing timing-sensitive
test failure and the aggregate correctly failed.

| Observation | Unsharded main | Four-shard PR |
| --- | ---: | ---: |
| Queue before runner start | 3 s | 6m52s–15m03s per shard |
| Setup before pytest | 1m12s | 49s–57s per shard |
| Pytest critical path | 1h06m31s | 34m20s |
| Sum of pytest execution | 1h06m31s | 1h03m44s |
| Complete-test wall clock from job creation through constituents | 1h07m51s | 42m39s |
| Sum of constituent runner occupancy | 1h07m48s | 1h07m14s |

The after values come from the four constituent job and step timestamps; the
aggregate then failed in four seconds after the last shard completed. Queue time
is reported independently and is not counted as setup or runner work. A later
green remote execution remains necessary before calling the partition qualified.

The complete-source matrix may bootstrap its estimated shard work from the
SHA/selector/unit-verified checked-in non-native duration snapshot when there is
no compatible native profile or only partial native measurements. This affects
packing, not collected membership, native pytest outcomes or exact-run/attempt
receipt validation. Planning diagnostics distinguish snapshot estimates from
native observations, runner queue, preparation and measured critical path.
