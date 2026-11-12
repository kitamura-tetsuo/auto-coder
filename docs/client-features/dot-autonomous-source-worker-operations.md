# Staged dot development and source-worker rollout (partial #2608)

This document is the operator runbook for #2608; it is not a new scheduler,
OAuth issuer, manager, Docker controller or publication authority. An operator
owns stage selection and current scopes. Dot never upgrades itself from
`observe` to `beta` or `full`, never grants itself consent, and never
substitutes a workflow run, a publication tag or a running container for an
actual installed source and advancing producer heartbeat. The non-interactive
read-only observer and the separate disposable acceptance client have distinct
roles. The client implements the positive beta-to-release sequence described
below; live producer evidence for the entire #2608 contract is still required.

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

The [schema-1 example checkpoint](../examples/management-mcp/dot-rollout-checkpoint.example.json) lists every required field. Copy it only into an operator-owned private location, populate original identifiers from verified sources, and never include bearer/refresh credentials.

The `schema=1` operator checkpoint contains only nonsecret identifiers:
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

**Current acceptance gap:** This read-only observer has no mutation mode. The
separate opt-in disposable harness implements beta source installation, beta
acceptance, exact Git source promotion and independent release activation.
Implementation and deterministic regression success do not establish live
OAuth, GitHub Actions publication, worker heartbeat, fault/recovery or scheduler
acceptance. Record each missing real scenario as **NOT_RUN/BLOCKED**, never
simulated `PASS`.
Production beta/release changes are prohibited. Keep this implementation PR
DRAFT until genuine end-to-end verification, exact-head CI and all review
threads have passed.

### Disposable-only acceptance harness

`scripts/dot_disposable_fixture.py` creates an owner-private, per-fixture Docker
Compose configuration using the fixed `kitamura-tetsuo/auto-coder-test`
repository, non-root workers, no host ports or runtime Docker socket, and an
observe-only management policy. It neither executes Compose nor creates grants.
The operator must separately provision and explicitly authorize a disposable
fixture before any live acceptance. The generated image names are pinned fixture
requirements, not automatically verified runtime identities. The Firebase ID
must start with `fixture-`, and authenticated `whoami` must return that **exact**
project ID. The test-source checkout must be a fully clean `main` of the fixed
test repository, including no untracked files. The fixture starts with an empty
UID allowlist and intentionally held worker bootstrap; both require separate
operator authorization and real test-only setup to proceed.

Both workers install source from `kitamura-tetsuo/auto-coder-test`. Workload
ownership stays distinct: beta processes only the fixed
`kitamura-tetsuo/auto-coder-test-beta` canary repository, while release processes
`kitamura-tetsuo/auto-coder-test`. Their instance IDs, runtime/workspace
directories and ownership records remain independent. Creating the canary
repository and providing its scoped read credential are explicit operator
bootstrap prerequisites. Published acceptance records bind the common source
repository and separately retain the actual beta workload repository. The idle
fixture enables its sole task-only Jules backend so the real CLI can initialize;
empty production Issue/PR author allowlists, disabled label changes and disabled
automatic/dependabot merge keep it from starting repository work. No LLM
credential is mounted. The held bootstrap must still be explicitly released by
the external fixture operator; no application acceptance is inferred from it.

For source promotion, additionally provide both
`--source-release-workflow-id` and `--accepted-profile-revision` to the generator.
These must identify the actual test repository workflow and acceptance profile.
They generate `/fixture/policy/source-release-policy.json`, bounded to the exact
fixture beta instance, and the corresponding management configuration. The
matching policy used by the real test repository Actions workflow must also be
reviewed. The default operating policy remains observe-only. The generated
credential files are empty and owner-private: `source-public-read` is the
independent Git/Actions reader and the release workload reader;
`beta-work-read` is mounted only in beta and reads only the fixed canary
workload repository. `source-acceptance-publish` authorizes only test
acceptance publication, and `source-release-dispatch` authorizes the test-only
promotion workflow. Current owner authorization and correctly scoped credentials
are required before these operations can run. Never copy production credentials.

```sh
python scripts/dot_disposable_fixture.py \
  --fixture-id pr2835-sandbox-20261112 \
  --firebase-project-id fixture-actual-sandbox \
  --test-source /path/to/clean/auto-coder-test
# Exit 3/NOT_RUN: this generates configuration only, not Docker or OAuth.
docker compose -f .agent-tmp/pr2835-sandbox-20261112/compose.yaml up -d
# Run only inside the generated management container after inspecting isolation:
docker compose -f .agent-tmp/pr2835-sandbox-20261112/compose.yaml exec -T management \
  python -m scripts.dot_mcp_acceptance --fixture-manifest /fixture/fixture.json
```

Without an **exact fixture-specific** `AUTO_CODER_MCP_ACCESS_TOKEN`, the client
returns `BLOCKED` before opening an MCP connection. An authenticated
`auto_coder_targets` result must contain exactly two distinct, complete
beta/release fixture bindings; duplicate IDs, omitted or extra entries, and
malformed targets refuse before any mutating request. Even with OAuth,
`observe` needs two independently advancing verified worker heartbeats. Both
samples for each worker must retain the exact original Supervisor generation,
process-run ID, runtime ID, deployment generation, and installed source binding.
A worker restart with a larger counter is not continuous heartbeat progress;
the client refuses it rather than claiming installed-source acceptance. The
read-only heartbeat sampler polls every 5 seconds for up to 150 seconds because
the idle producer's maintenance heartbeat may advance only once per 60 seconds.
Every sampled worker is rechecked until all have advanced, so an earlier worker
cannot disappear during a later worker's wait. The same two-sample rule gates
`beta-source` and `release-source` completion. Mere container liveness,
accepted MCP receipts or a copied Git SHA are insufficient.

`scripts/dot_mcp_acceptance.py` is opt-in and returns `NOT_RUN` without a fixture.
Its default path only reads current authenticated MCP observations. The explicit
`--enable-fixture-mutation` mode requires the current selected stage/revision,
original Git/Actions proof and an immutable fsynced pre-send ticket. It exposes
only the following fixed actions against the exact disposable targets:

| Scenario | Required operator stage | Independent completion evidence |
| --- | --- | --- |
| `reevaluate` | beta or full | Original worker invalidation receipt; never Issue completion |
| `beta-source` | beta or full | Confirmed original deployment, exact installed source and advancing same-child heartbeat |
| `beta-acceptance` | beta or full | Original deployed beta child, real `PASS_PUBLISHED` receipt and independently read immutable acceptance Git ref/OID |
| `promote-source` | full | Original tested-source/acceptance chain, successful exact Actions attempt, immutable release catalog and current source/operation selection; release worker deployment remains separate |
| `release-source` | full | Original promotion/catalog/acceptance linkage, independent release deployment receipt and exact installed release child with advancing heartbeat |

The first full-stage promotion does not require a fabricated preexisting
promotion catalog. It requires the original confirmed beta acceptance and
current full authority; its successful result supplies the promotion evidence
needed by the separate release activation. A green promotion cannot report
`positive_deployment_proven=true` or `release_worker_deployed=true`.

Each scenario has one private original-send ticket. The next permitted action
uses its own immutable idempotency key; it cannot reuse a key from another action
on the same target. Any unresolved same-target ticket blocks a new action even
with a different key. Independent fixture clients serialize original-ticket
admission through one private, stable Linux file lock in the management fixture
directory, covering the prior-ticket scan and durable ticket creation across
different action names. A missing, replaced or unprotected lock refuses before
a mutation; the lock is not a management grant or a substitute for the producer
journal. A negative probe can release that local fence only after the actual
refusal plus independent real-journal absence has been recorded as
`REFUSED`; a refusal response by itself is insufficient. BUSY/CONFLICT responses
can include an occupying operation ID, which is never adopted as this send's ID.

The initial checkpoint supplies the original task/repository/Issue/PR, source,
CI and operator selection. Subsequent scenarios can take deployment/acceptance/
promotion IDs from that checkpoint or the same fixture's completed original
tickets. The client re-queries the actual producer and Git objects before using
them. Successful JSON `details` records the exact derived refs/OIDs and worker
identities; the same evidence is durably appended to the original ticket.

Use `--resume-original` with the same scenario, checkpoint authority and key to
continue a known original operation after interruption. The client loads its
saved arguments and ID before transport and never repeats admission. An unknown
admission without a saved original ID remains blocked. `--wait-seconds` defaults
to 300 and is limited to 900; a timeout preserves the original instead of
creating another operation. For example, inside the fixture management container:

```sh
python -m scripts.dot_mcp_acceptance \
  --fixture-manifest /fixture/fixture.json \
  --checkpoint /fixture/management/original-beta-source.json \
  --scenario beta-source --enable-fixture-mutation \
  --operator-stage beta --operator-stage-revision ORIGINAL_OPERATOR_REVISION

python -m scripts.dot_mcp_acceptance \
  --fixture-manifest /fixture/fixture.json \
  --checkpoint /fixture/management/original-beta-source.json \
  --scenario beta-source --enable-fixture-mutation --resume-original \
  --operator-stage beta --operator-stage-revision ORIGINAL_OPERATOR_REVISION
```

The source promotion producer currently needs an explicit original-operation
reconciliation to observe its dispatched Actions run. Only the authorized
`promote-source` mutation scenario can call `auto_coder_operation_get`, bound to
its already-persisted exact target and operation ID, and only after the producer
reports `effect_state=sent_unknown`. It never resubmits the promotion or changes
its key. Default observation and `inspect-original` exclusively use the
non-dispatching reader. Original journal inspection runs before worker
diagnostics, so a failed beta worker cannot hide an original release operation.

Unknown, interrupted or conflicting sends remain `BLOCKED`; the original
idempotency key is never reissued. A transport exception after a durable
original send ticket cannot report `effect_dispatched=false`: the report
carries `effect_dispatched=null`, the original ticket state and verified original
operation ID when available. Pre-send refusal without a ticket reports false;
corrupt, replaced or unreadable tickets report UNKNOWN, not a guessed absence.
Before a new send, the client also reads the companion's durable
`dot-recovery-<failure-signature>.json` records in the same management state
directory. For the same action, target and source, it inspects the exact retained
original operation without reconciliation and verifies the producer's consumed
recovery count and original failure. A confirmed terminal `FAILED` operation
may have released the service's target lane, but it does not reset this client
budget. A new checkpoint, operator revision or idempotency key cannot bypass it,
even when the original was admitted outside this client and has no send ticket.
Missing or changed original evidence remains blocked; the retained record is
never cleared automatically. An unrelated action, target or source does not
inherit that exhausted signature. Observation and explicit original resumption
remain available without admitting another operation.
Live producer observations are hashed for origin tracking. A real reevaluation journal projects terminal acceptance as
`status=accepted_for_reevaluation`, `phase=succeeded` and
`effect_state=confirmed` with a JSON-encoded
`worker-uds-invalidation-receipt`. The consumer verifies that receipt's
original process-run identity before reporting queue acceptance; this never
asserts Issue/PR implementation completion. A generic `SUCCEEDED` status or
an uncorrelated receipt cannot satisfy this scenario. No successful synthetic
fixture test certifies live acceptance.

These scripts provide reusable acceptance components, not a completed #2608
contract: obtaining real fixture grants, actual controller/worker/source and
GitHub Actions evidence, staged unknown-effect/recovery regressions, and dot
scheduler recurrence still require independent end-to-end verification. Never
use this harness against production or existing beta/release services or treat
its local unit tests as deployment, authorization or merge proof.

The separate [negative acceptance procedure](dot-negative-acceptance.md) covers
actual observe/policy refusals and original-ticket/held-drain/recovery evidence.
Its real-journal checks can establish a refused admission without removing an
unresolved effect or inventing an operation ID. Keep these negative results
separate from the positive beta-to-release records and scheduler capability
evidence; none substitutes for another.
