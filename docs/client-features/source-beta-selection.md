# Beta selected Git source

The operator-dispatched `Advance Beta Source` workflow independently selects a
Git revision for workers that update Python code inside an existing container.
It does not build an image, inspect or move OCI tags, install code, restart a
worker, or assert that a running worker has installed the selected revision.
The existing image `Advance Beta` workflow remains the independent bootstrap
publication path.

The trusted repository is the workflow's own `${{ github.repository }}`. The
source workflow is fixed to `Test Source for Runtime` on `main`. Eligibility is
derived from exact successful Actions run attempts, all required jobs, and the
maximum required-job `completed_at`. Missing or partial observations, a tie for
the greatest completion time, and a latest execution without its immutable
`auto-coder-tested-<SHA>` annotated tag all fail closed.

The mutable direct-commit pointer is
`refs/tags/auto-coder-beta-current`. Before changing it, an operation creates
and reads back the create-only annotated history ref
`refs/tags/auto-coder-beta-selection-r<RUN_ID>`. Actions reruns keep the same
originating workflow run ID and therefore reconcile the same operation. Its canonical
JSON annotation contains the selected SHA and exact run/attempt/completion,
the tested-source ref/object and its original proof-producing run/attempt, the
expected previous pointer, repository, and operation identity. The annotated
tag is anchored directly to the selected commit. Workflow retries reuse that
record and never resample current latest evidence.

Before the first pointer request, the selector rechecks latest eligibility and
creates the immutable `...-pointer-attempt` record. That record means a pointer
request may already have been sent; recovery performs readback but never resends
merely because the mutable ref is absent or temporarily unavailable.

The workflow requires `actions: read` and `contents: write`. Its stable Actions
concurrency group serializes supported workflow writers; this is not a global
compare-and-swap promise against administrators or other trusted direct Git ref
writers. Exact readback reconciles a lost write response. Missing readback stays
uncertain, conflicting immutable history fails, and an unexpectedly superseded
pointer is reported rather than overwritten. Selection success means only that
the history and current Git ref resolve to the verified SHA; installed worker
source remains a separate runtime observation.
