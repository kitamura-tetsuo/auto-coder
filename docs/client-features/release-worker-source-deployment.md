# Authenticated release worker source deployment (pending real-host acceptance)

The `auto_coder_deploy_release_source` management MCP action is implemented as
an explicit, instance-bound request using the existing OAuth read+deploy grant,
the operator-owned `full` release policy, the shared per-instance operation
journal, immutable version preparer and signed Supervisor control lane. It is
not an automatic consequence of promotion or an OCI/Docker deployment.
**PR #2832 remains draft until genuine independent release-worker and
previous-version host acceptance is recorded.** Controlled tests do not
establish that a real release worker has been deployed or is healthy.

## Trust boundaries and request

The operator configures the release worker target, fixed repository, private
Supervisor endpoint and secret, promotion workflow ID, source-release policy
and GitHub credential file. The MCP caller cannot select any of those, or a
command, shell, runtime path, image, Docker container or executable. The
target must have channel `release`, match the promotion repository and remain
permitted for `ActionClass.DEPLOYMENT` by the current `full` policy. The
original accepted OAuth grant must still include management read and deploy
scopes before each newly dispatched worker effect.

Supply all these parameters to the authenticated MCP tool:

| Argument | Required identity |
| --- | --- |
| `target_id` | Operator-configured release worker instance alias |
| `selected_git_sha` | Exact lowercase 40-hex Git commit |
| `promotion_operation_id` | Original successful accepted-source promotion management operation |
| `catalog_ref`, `catalog_oid` | Immutable `auto-coder-source-release-<operation_id>` annotated catalog ref and object OID |
| `acceptance_ref`, `acceptance_oid` | Original exact published source-acceptance ref/object |
| `idempotency_key` | Stable key; reuse unchanged after lost replies |
| `expected_supervisor_generation` | Current signed Supervisor incarnation |
| `expected_process_run_id`, `expected_deployment_generation` | Current running child's signed OS-lifetime identity, or both omitted when stopped |
| `expected_runtime_id` | Current selected installed runtime identity |

The read-only `ReleaseWorkerEvidenceReader` joins the original succeeded
management promotion, its immutable journal intent/admission, bound successful
Actions attempt, published acceptance, tested source record, schema-2 catalog,
GitHub Release and source-channel current selection. Independent observations
are repeated before acceptance. Expired evidence, stale or missing refs, a
different repository, changed policy or original operation, and a later
promotion of **the same SHA** fail closed. This reader by itself never grants
deployment permission or dispatches worker effects.

The release source commit ref
`refs/tags/auto-coder-release-source-current` is paired with
`refs/tags/auto-coder-release-source-selection-current`, whose target is the
annotated catalog object identifying the selecting promotion operation. Every
promotion advances the operation-selection ref after confirming the direct
source commit ref. The release writer must remain serialized through the
existing `release-channel` Actions concurrency lane. GitHub does not offer
an atomic transaction across those refs, GitHub Releases and the journal:
ambiguous writes, unrelated external writers and incomplete postconditions
require operator investigation, never optimistic replay.

## Lifecycle and readback

1. Complete the separately authorized accepted-source promotion. Verify its
   `SUCCEEDED` management operation, immutable catalog and original acceptance
   OIDs. Record the release worker's signed Supervisor, process and selected
   runtime identities.
2. Invoke `auto_coder_deploy_release_source` with the original facts and a
   stable idempotency key. Identical same-key requests return the original
   operation; changed-intent reuse returns `CONFLICT`. A competing key for the
   active release instance returns `BUSY`, even if source selection changes.
3. Poll `auto_coder_operation_get` with the exact `target_id` and returned
   `operation_id`. `PREPARING` stages the candidate before any drain. Exact
   accepted SHA, lock digest and version must equal the immutable preparer's
   durable `READY` artifact. The exact selected promotion and original
   authority are checked again immediately before the **first** replacement.
4. `DRAINING` requires the original Supervisor's correlated terminal graceful
   receipt and independent observation of the incumbent's real OS exit.
   `STARTING` verifies the old lifetime is gone and selects only the
   independently installed runtime ID. No force kill, container restart,
   host reboot or peer beta worker mutation is authorized.
5. `RECONCILING` accepts only the genuine new process run and generation,
   verified runtime binding, build-embedded source SHA, installed distribution
   metadata, startup reconciliation, serving daemon, running invocation gate
   and at least two strictly advancing producer-origin heartbeat sequences.
   A launch receipt alone never establishes `SUCCEEDED`.

Each possibly sent step persists the original journal attempt, owner
generation, correlation, fixed Supervisor operation ID and digest **before**
dispatch. Lost replies are reconciled by the original durable receipt.
Current policy/grant loss, missing selection, ambiguous process exit and
unresolved original operations retain the same journal lane; no timeout
permits replacing them with a different request.

If the new child fails after activation, recovery may start **at most one**
previously verified prepared runtime, with a separately persisted step,
after actual new-child exit and with current authority. The readback exposes
`recovery_attempts`, `recovery_state` (`NOT_ATTEMPTED`, `ATTEMPTED`,
`UNKNOWN` for an unsettled step, `CONFIRMED`) and `restored_worker`.
Confirmed recovery ends new-source
deployment as `FAILED`, not `SUCCEEDED`. The release readback retains
`new_worker_failed_previous_runtime_restored` while existing beta readback
retains `new_beta_worker_failed_previous_runtime_restored`, so release
admission does not silently change beta failure diagnostics. Unknown recovery
is not automatically retried. Restoring Python cannot undo migrations, database
state, provider effects or publication; those require explicit operator
rollback/bootstrap investigation.

## Verification boundary

`tests/test_release_worker_deployment.py` exercises original promotion
and acceptance, authenticated MCP admission, same-SHA supersession,
replay/CONFLICT/BUSY, policy changes, lost stop receipts, prior-runtime
recovery and rejection of worker artifact fields with a controlled
Supervisor bridge. `tests/test_management_worker_lifecycle.py` and
`tests/test_worker_supervisor_bridge.py` separately exercise actual
subprocess, UDS, HTTP, held drain, OS exit and Supervisor reconstruction.
These are **not** a substitute for a complete production-origin release run
with a real installed candidate, an independently running prior version,
held exit/recovery and peer beta isolation. Record the exact original
positive-control evidence, CI results and operator acceptance against
Issue #2613 before marking the PR ready to merge.

A bounded regression command is:

```bash
uv run pytest tests/test_release_worker_deployment.py tests/test_management_source_release_dispatch.py tests/test_source_release_promotion.py
```

There is no new dashboard scheduler/provider event, Docker socket or runtime
host authority. Existing management Loguru sinks record admission and
reconciliation. Related observability is in `docs/dashboard-observability.md`.

### Native release bootstrap reconstruction regression

The authenticated bootstrap START and RESTART lost-response regression also runs
with a release target under the full operator policy. It crosses the real signed
HTTP bridge, Supervisor, worker Unix socket and OS child boundary, then reconstructs
the manager and Supervisor and verifies that the original operation and exact
child lifetime are retained without a second START dispatch. A bootstrap-only
child still cannot report SUCCEEDED: this is not installed accepted-source
release activation evidence and does not replace the remaining integrated
REQ-009/010/011 acceptance gates.
