# Staged dot development and source-worker rollout (partial #2608)

This document is the operator runbook for #2608; it is not a new scheduler,
OAuth issuer, manager, Docker controller or publication authority. An operator
owns stage selection and current scopes. Dot never upgrades itself from
`observe` to `beta` or `full`, never grants itself consent, and never
substitutes a workflow run, a publication tag or a running container for an
actual installed source and advancing producer heartbeat. The non-interactive
read-only observer below is **a partial implementation**, not the required
live end-to-end acceptance harness.

## Bootstrap and authority inventory

Before starting a stage, independently verify that a durable dot recurring
execution mechanism actually invokes the intended job, that its notifications
and GitHub integration are callable, and that the configured management
`/mcp` transport is reachable with current original OAuth consent and policy.
Record `NOT_RUN` for an unattempted capability and `BLOCKED` for an
unavailable or untrusted one. A successful OAuth refresh neither extends
the original grant's **30-day absolute maximum** nor guarantees future
unattended operation. An absent scheduler, management service, private
worker network, current grant, source credential or exact original identity
is not an implicit approval for another transport or effect.

Initial provision is an **operator-only** one-time checklist:
set exact Compose channel network, fixed management and per-channel Supervisor
services, pinned Git/uv/Python for runtime staging, each private machine
credential, persistent management/OAuth/journal directories and separate
version roots. The operator (not the MCP child) uses `docker compose` for
initial provisioning; recurring MCP operations must not use Docker CLI,
socket/API, image replacement or pulls. The release-only, approved
one-shot candidate recovery may rely on the existing Docker
`restart: unless-stopped` policy after a correlated Supervisor PID-1 exit
inside that **same** container; management never invokes Docker itself.

Read the authoritative service contracts:
[management-runtime-mcp](management-runtime-mcp.md),
[management-operation-journal](management-operation-journal.md),
[management-oauth-grants](management-oauth-grants.md),
[source-beta-selection](source-beta-selection.md),
[beta-runtime-smoke-evidence](beta-runtime-smoke-evidence.md),
[management-source-release-dispatch](management-source-release-dispatch.md),
[accepted-source-release-promotion](accepted-source-release-promotion.md) and
[release-worker-source-deployment](release-worker-source-deployment.md).

## Ordered stages and independent evidence

| Operator stage | Permitted behavior | Required positive evidence before advancing |
| --- | --- | --- |
| Observe | Authenticated reads only: `auto_coder_whoami`, targets/status, diagnostics and original operation readback | Current configured target and actual signed worker/child lifetime, startup reconciliation, installed source and advancing producer samples; no effects |
| Beta | Observe, then separately authorized configured Issue/PR reevaluation, exact selected tested-source install into the existing beta worker and beta acceptance | Durable reevaluation acceptance (not Issue/PR completion); exact Git SHA and successful original CI run/attempt; selected Git source ref/object; installed package/source/lock/runtime/worker generation; startup and advancing beta heartbeat; original acceptance operation and published immutable ref/OID |
| Full | All beta gates, followed by separately authorized accepted-source promotion and separately authorized release-worker installation | Current exact successful promotion operation, Actions run/attempt, catalog/ref/object and selection, then independently installed release runtime SHA, Supervisor generation, native process and advancing heartbeat; a published source catalog alone never means deployed release |

Normal development uses authoritative GitHub Issue dependencies, open PRs,
exact-head required CI, actionable review threads and mergeability. Never
allocate implementation against a tracking-only/blocked Issue. The
Issue/PR implementation and merged SHA become a *candidate*, not deployment
evidence. The accepted source and release current refs have independent
immutability/selection requirements; a later promotion of the same SHA can
supersede an earlier selection.

## Resumable checkpoint and finite recovery

The [schema-1 example checkpoint](../examples/management-mcp/dot-rollout-checkpoint.example.json) lists every required field. Copy it only into an operator-owned private location, populate original identifiers from verified sources, and never include bearer/refresh credentials.\nThe `schema=1` operator checkpoint contains only nonsecret identifiers:
operator `stage`, original task ID, repository and Issue/PR numbers, immutable
operator-selected stage revision, exact source SHA and selected CI run/attempt/tested
ref and OID, configured beta/release targets, original management operation IDs,
installed runtime IDs and generations, acceptance ref/OID, promotion
operation/run/catalog ref/OID, stable original idempotency key, and
`pending_effect_state`, exact `pending_operation_id`, immutable failure signature and its recovery count.
This is a **reminder**, never a grant, proof of an effect or a current worker
observation. The original CI run/attempt are positive canonical decimal identifiers and the tested,
accepted-beta and promotion Git OIDs are exact 40-character lowercase object IDs;
malformed checkpoint identifiers fail closed before any request.
Full-stage diagnostics additionally require the distinct original configured
release worker target: a promotion record without a selected release target
cannot even establish which installed runtime remains to be checked.
The beta and release target identities must be distinct whenever both are
recorded. A shared target cannot be interpreted as separate release-worker
activation, even with valid Git promotion evidence. Saved checkpoints,
offline planning and live read plans refuse this configuration.
Beta/full work without the original task, repository, Issue/PR
identity or stage revision is BLOCKED. Before a beta or full stage can
report even a diagnostic-only `NOT_RUN`, the checkpoint must also retain the
selected source SHA and ref/OID, original CI run/attempt and configured beta
target; absent upstream lineage is `BLOCKED` rather than silently skipped in
full mode. These retained values must be compared
with fresh operator authorization before any future mutation; current MCP read
diagnostics cannot attest to a dot stage revision and never elevate it.
The worker and original journal MUST be re-queried after
interruption; no current HEAD substitution, changed-intent same-key replay
or new idempotency key after an unknown send.

`SENT_UNKNOWN` and `NEEDS_ATTENTION` preserve the original operation and
the per-target lane. Reconcile only by the exact original operation ID
already retained in the checkpoint and uniquely bound to its configured
beta/release target (release promotion reads use the release target). The
pending operation ID is stored separately from all completed operation IDs:
when a pending effect exists, CLI readback MUST select that exact ID, even if
another recorded operation belongs to the same target. A
different target, ambiguous reused operation ID, unbound target or
unrecognized nonempty pending-effect state fails closed before transport.
The same unique original-operation/target binding is required for any optional
operation readback, even without an unresolved send; a diagnostic request
must not select an unrelated operation from the service.
When reconciliation is pending, the read plan requests original-operation
readback immediately after identity and before deployment diagnostics; an
unrelated or newly invented ID refuses even a bounded read plan. Never infer
no effect from elapsed time or silence. In any unchanged action, target, source
and failure-signature namespace, use at most one new authorized recovery
operation, then mark `BLOCKED` and notify the operator. Never bypass
the existing slot, retry/EXHAUSTED, merge or authorization gates.

Stop, preserve the original records, and escalate for: grant revocation or
expiry; policy downgrade; missing source/Git catalog/selected ref or installed
worker provenance; held genuine graceful drain or surviving old OS lifetime;
inaccessible private network; incompatible/failed uv/Python staging;
untrustworthy original receipt, actor or Supervisor; repeated failure;
superseded release selection; unresolved recovered runtime; or migration/schema
incompatibility. One permitted prior **installed runtime** reactivation is
not a rollback of data, databases, migrations, filesystem state, provider tasks,
remote effects or already published GitHub objects. There is no force-restart,
arbitrary command, emergency promotion or Docker control fallback.

## Bounded observer, safety and exact outputs

`scripts/management_mcp_rollout_smoke.py` accepts an operator-created
**full schema-1 checkpoint**. Its default is pure local inspection and
`NOT_RUN`; it makes no network calls unless an explicit `--endpoint` is
provided. The optional live client uses the pinned MCP SDK and only an exact
readonly allowlist: `auto_coder_whoami`, `auto_coder_status`,
`auto_coder_deployment_status`, and original non-dispatching
`auto_coder_operation_inspect`.
The only bearer source is `AUTO_CODER_MCP_ACCESS_TOKEN`, never CLI arguments,
checkpoint, stdout, report or logs. The endpoint must be HTTPS ending in
`/mcp` (or HTTP for explicit loopback). Never point it at a deployed
service during disposable tests. Before any live diagnostic using a beta/full
checkpoint, the operator must newly supply `--operator-stage` and
`--operator-stage-revision`. They must exactly match the original saved stage
and revision; missing, changed, or invalid revisions are `BLOCKED` before
network access. The live beta/full client also checks the checkpoint's original work, tested
source SHA/ref/OID, CI run/attempt, configured beta target and remaining
recovery budget **before opening network transport**. Without these original
bindings it returns BLOCKED, not a live read against an incomplete plan.
The observe-only default needs no fresh revision. This check
prevents a stale checkpoint from silently selecting a different stage; it is
not server attestation or an authorization grant. Read-only target diagnostics
are limited to the checkpoint's selected beta target in beta stage and its
configured beta/release targets in full stage. Observe stage may select a
fresh explicit target only when no targets were saved. A pending unknown effect
is instead read back from its exact original operation-to-target binding,
including when the current saved stage differs; this read never authorizes a
new effect. The current authenticated MCP policy/grant and live producer
evidence remain separately necessary.

Example offline invocation on a machine with the project environment:

```sh
python scripts/management_mcp_rollout_smoke.py
```

For **later operator-authorized nonproduction** authenticated read-only checks:

```sh
AUTO_CODER_MCP_ACCESS_TOKEN=... python scripts/management_mcp_rollout_smoke.py \
  --checkpoint /private/operator-selected-dot-checkpoint.json \
  --operator-stage beta --operator-stage-revision ORIGINAL_OPERATOR_REVISION \
  --endpoint https://disposable-fixture.example/mcp \
  --target-id fixture-beta-worker --operation-id ORIGINAL_OPERATION_ID
```

The example domain and IDs are placeholders, not usable configured resources.
The client refuses unknown tool names or mutation requests and never issues
effects. The legacy `auto_coder_operation_get` can advance a pending worker/source
operation via the dispatcher despite its read-facing name and scope. The
separate `auto_coder_operation_inspect` reads only the authenticated owner's
original journal projection, including the immutable operation ID, target,
effect state and already-recorded receipt; it never enqueues, submits or
settles an effect. The observer plans the exact pending ID and original target,
then uses only this non-dispatching reader. A server without that tool reports
`BLOCKED`, not an alternative call to the effectful API. A diagnostic receipt
is not by itself proof of current worker installation, source publication or
successful bounded recovery. The real fixture acceptance flow remains a
separate REQ-007/008 obligation.

The observer reports `BLOCKED` for invalid/missing authority or uncertain
original state and `NOT_RUN` for a diagnostic-only observation; digests
of actual structured MCP read responses identify the observed bytes without
leaking raw credentials or implying installed readiness. `positive_deployment_proven`
and `effect_dispatched` remain false. Stdout is newline-delimited JSON (Loguru
console records followed by the final report on the last line); the same
nonsecret diagnostics go to a rotating Loguru state-directory file. Exit codes
are 0 only for independently established `PASS`, 1 for `FAIL`, 2 for
`BLOCKED` and **3 for `NOT_RUN`**, so a scheduler cannot mistake an
unexecuted acceptance for success. Any future `PASS` requires an independent original producer oracle,
not just an MCP JSON response or a checkpoint.

**Current acceptance gap:** This version has no mutation mode, no
operator-selected disposable Docker/source repo test fixture provider, no real
authenticated beta/release effect flow, no independent Actions/source acceptance
cross-check, and no verified real dot scheduling exercise. Those
#2608/REQ-007/008/011 deliverables are explicitly **NOT_RUN/BLOCKED**, not
simulated `PASS`. Changes to the running beta/release service are prohibited
for this test. The implementation PR stays DRAFT until the genuine disposable
end-to-end staging/unknown/recovery/policy downgrade tests and exact-head CI
succeed and all review is resolved.

### Disposable-only acceptance harness (partial)

`scripts/dot_disposable_fixture.py` creates an owner-private, per-fixture Docker
Compose configuration using the fixed `kitamura-tetsuo/auto-coder-test`
repository, non-root workers, no host ports or runtime Docker socket, and an
observe-only management policy. It neither executes Compose nor creates grants.
The operator must separately provision and explicitly authorize a disposable
fixture before any live acceptance. The generated image names are pinned fixture
requirements, not automatically verified runtime identities.

`scripts/dot_mcp_acceptance.py` is opt-in and returns `NOT_RUN` without a fixture.
Its default path only reads current authenticated MCP observations. The explicit
`--enable-fixture-mutation` mode confines available actions to test-only beta
reevaluation or beta-source selection and requires current selected stage,
revision, original Git/Actions proof and an immutable fsynced pre-send ticket.
Unknown, interrupted or conflicting sends remain `BLOCKED`; the original
idempotency key is never reissued. Live producer observations are hashed for
origin tracking. No successful synthetic fixture test certifies live acceptance.

These scripts provide reusable acceptance components, not a completed #2608
contract: obtaining real fixture grants, actual controller/worker/source and
GitHub Actions evidence, staged unknown-effect/recovery regressions, and dot
scheduler recurrence still require independent end-to-end verification. Never
use this harness against production or existing beta/release services or treat
its local unit tests as deployment, authorization or merge proof.
