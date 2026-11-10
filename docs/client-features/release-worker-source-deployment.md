# Release worker source deployment: draft provenance join

Issue #2613 is **incomplete and must remain draft**. There is no authenticated
release-worker deployment tool in this change. Accepted source promotion and
worker activation remain separate authority and evidence boundaries. No worker,
original host, container or release has been deployed or verified by this work.

`management/release_worker_deployment.py` supplies an internal, read-only
`ReleaseWorkerEvidenceReader`. Its trusted dependencies are the existing
management operation journal, configured GitHub transport, source-release policy,
promotion target alias and workflow ID. Its exact request contains the original
management promotion operation ID, full lowercase Git SHA, immutable catalog
ref/object OID, and acceptance ref/object OID. It accepts no endpoint, repository,
credential, executable, path, Docker name, OCI image or command from the caller.

The reader requires a successful, confirmed original management promotion and its
unchanged intent, persisted admission snapshot, original bound Actions identity
and correlated source-publication receipt. It independently reads the successful
original Actions attempt, reuses `AcceptedSourceValidator` for original CI and
published acceptance, verifies the exact schema-2 catalog through the existing
promotion producer, and repeats catalog/Release/current-source readback. New
observation uses the current evidence clock, so historical promotion success
cannot make expired acceptance eligible for a new deployment. Missing, stale,
unpublished, mismatched, superseded or unavailable evidence raises
`SourceReleaseError`. The reader neither dispatches effects nor updates journal
operations, accepted authority, source refs or catalogs.

These are provenance facts only. The direct release source ref identifies a
commit; it does not identify the selecting promotion operation. A later accepted
promotion of the same commit leaves that pointer equal. Consequently every
returned record explicitly has `release_operation_selection_proven=false`,
`deployment_authorized=false` and `worker_deployed=false`. Consumers must not
treat source equality, a successful workflow or this readback as activation
authority. This change does not introduce a new trust assumption about external
Git-ref writers or certify an atomic multi-resource selection.

## Pending implementation and acceptance gates

No REQ-001 through REQ-011 is claimed complete. The reader contributes the
immutable provenance join to REQ-003/004; the following work remains:

* REQ-001/002/004: register the bounded authenticated action with the configured
  release instance and existing shared per-instance journal lane. Recover
  immutable same-key intent before resolving selection, conflict on changed
  intent, retain BUSY for another key, and recheck the original OAuth read+deploy
  grant and current full operator policy before every new worker-changing effect.
* REQ-003/004: establish operation-specific current-release selection, including
  later same-SHA promotion, without substituting a newer release. Revalidate the
  exact binding immediately before first drain or stopped-child pointer/start;
  never reinterpret a possibly sent effect as unsent after selection changes.
* REQ-005/006: integrate the established immutable version preparer and Supervisor
  algorithm, including prepared-runtime reconstruction. Durably confirm original
  operation/owner generation/attempt/action correlation before each dispatch.
  Reconcile exact evidence after lost replies or manager/Supervisor restart;
  unknown effects retain ownership without timeout-based lane release. Preserve
  genuine terminal drain plus exact old OS lifetime exit before a successor and
  independently prove absence for an originally stopped child. No force-kill or
  unsettled-invocation cancellation is authorized.
* REQ-007/008: implement at most one newly authorized prior-prepared-runtime
  recovery, report attempted/confirmed/unknown separately, and establish actual
  installed package source, Supervisor/worker generation, startup reconciliation,
  daemon serving and genuine advancing producer heartbeat before success.
* REQ-009/010: prove peer beta preservation, unchanged ownership/publication gates,
  and no Docker/runtime-host access through production-origin tests with a real
  independent worker and prior version. Required positive scenarios include held
  drain and held OS exit, failed activation/recovery, lost effects, unconfirmed
  pre-effect persistence, selection changes after staging, original replay,
  changed-intent conflict, competing-key BUSY, and independent manager/Supervisor
  reconstruction. The current component tests do not supply those verdicts.
* REQ-011: complete the operator activation/readback/escalation procedure after
  the action and its normative tests exist. Keep the work draft until then.

Restoring a Python runtime cannot undo persistent state, database migrations,
remote provider effects or published changes. Unsupported native dependencies,
one-way migrations, missing original evidence, unproven process exit and ambiguous
recovery require explicit operator investigation/bootstrap/restore. Do not clear
an unresolved operation, retry with a new key to bypass ownership, change policy
automatically or grant Docker access as a workaround.

Run `bash scripts/test.sh --ci-tests-only tests/test_release_worker_deployment.py
tests/test_management_source_release_dispatch.py` for the bounded component
regressions. The new reader tests exercise the acceptance/promotion and dispatch
readback producers against controlled GitHub transport and a real SQLite journal;
their original authority metadata and worker/Actions observations are fixture
inputs, not authenticated worker evidence. The real-Git case creates isolated
annotated objects/refs and reconstructs the journal. The existing dispatch suite
separately exercises production HTTPS MCP/OAuth admission. GitHub Actions and
worker execution remain independent acceptance gates; these tests contact no live
management service and dispatch no live GitHub workflow.

This internal reader introduces no scheduler/provider/dashboard event or
deployment status. Existing management admission logging continues through the
application's Loguru standard-output and rotating-file sinks; no new logger or
credential logging is added. See `docs/dashboard-observability.md` for the
observability-neutral rationale and runnable coverage.
