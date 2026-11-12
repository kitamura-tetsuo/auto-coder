# Disposable dot refusal and bounded recovery acceptance

The negative scenario companion to
[the staged dot runbook](dot-autonomous-source-worker-operations.md) is
`scripts/dot_mcp_negative_acceptance.py`. It checks the specific refusal and
stop boundaries in #2608/REQ-004/007/008/010/011. It is not a new management
controller, a policy editor, a recovery dispatcher or a data rollback feature.

Without `--fixture-manifest`, execution returns `NOT_RUN` (exit 3), opens no
network connection and sends no effect. With a manifest, it enforces the same
private container nonce, fixed test repository, physical beta/release targets,
Firebase project, current OAuth identity and scopes as the positive acceptance
client. The checkpoint retains both disposable targets and the exact source.
The current operator stage and revision must match the saved selection.
Bearer values come only from `AUTO_CODER_MCP_ACCESS_TOKEN`, never arguments,
checkpoints, reports or logs. Nonsecret Loguru diagnostics go to stdout and a
rotating file; the last stdout line is the JSON report.

## Executable scenarios

| Scenario | Original producer evidence | Result and limit |
| --- | --- | --- |
| `observe-refusal` | The externally selected exact two-target `observe` policy, current real worker fence, authenticated `DENIED/profile_denies_mutation`, and an independent read-only SQLite lookup proving the original request key was never admitted | Negative boundary `PASS`; no deployment claim. The one fixed reevaluation probe requires explicit `--enable-refusal-probe`. |
| `policy-downgrade` | All observe-refusal evidence plus the existing successful reevaluation ticket and its real owned `accepted_for_reevaluation` worker receipt | Negative boundary `PASS`; proves earlier acceptance and current refusal. A saved stage name alone cannot prove a downgrade. |
| `beta-release-refusal` | Current operator stage and exact two-target policy both remain `beta`; genuine tested-source and published beta acceptance prerequisites are independently read before actual `DENIED/release_policy_denied`; the original release request key is absent from the admission journal | Negative boundary `PASS`; the fixed promotion probe requires `--enable-refusal-probe`. It does not construct a release catalog or claim release activation. |
| `held-drain` | Exact original lifecycle operation remains `DRAINING`, its original drain step exists with no start, and current signed Supervisor projection still identifies the same live incumbent process/runtime | Operational `BLOCKED` with `negative_boundary_verified=true`; no start, retry or recovery. |
| `unknown-effect` | Two non-dispatching reads of the same retained original worker, beta acceptance or source promotion operation and target continue to show the unresolved producer outcome | Operational `BLOCKED` with the negative boundary verified; an unknown original effect is never reported completed or unsent. |
| `repeated-failure` | The original producer reports one consumed recovery attempt and a held/failed/restored-prior-runtime result; its action, target, exact source and failure category are retained durably | Operational `BLOCKED`. The original failure signature remains exhausted across restarts and cannot be rebound to an invented retry operation. |
| `data-rollback` | Non-dispatching original runtime/lifecycle evidence | `BLOCKED`. No supported producer certifies persistent-data compatibility or restoration. Even a confirmed prior Python runtime restoration cannot establish that databases, files, migrations or external effects were rolled back. Escalate to the operator and keep admission suspended. |

A `PASS` in this companion means that a specifically named refusal assertion
was demonstrated. `positive_deployment_proven` always remains false. An unknown
operation, exhausted budget or unsupported data restoration retains an
operational `BLOCKED` verdict even when the stop boundary is independently
verified. No result here replaces positive beta acceptance, Git publication,
release activation or scheduler evidence.

## Refusal probe protocol

The immutable Python source and release workload use
`kitamura-tetsuo/auto-coder-test`; the beta workload uses the distinct
`kitamura-tetsuo/auto-coder-test-beta`. Both physical targets retain exclusive
repository ownership. The source proof is not a grant to process that source
repository concurrently in both channels.

For `observe-refusal` and `policy-downgrade`, an external operator first selects
the fixture's `observe` policy. The client reads that policy but cannot change
it. These explicit probes attempt only a fixed beta Issue reevaluation on
`kitamura-tetsuo/auto-coder-test-beta`.
The request uses current original worker fences and a persisted idempotency
key. A pending original operation or consumed recovery budget prevents even
this negative probe.

The separate `beta-release-refusal` probe requires an explicitly selected
`beta` stage and current `beta` policy. It reuses the positive client's genuine
tested-source and published beta-acceptance checks, then sends one fixed
`auto_coder_promote_accepted_source_release` request to the configured release
target. This tests the actual producer's beta-only policy boundary after valid
prerequisites; it needs no fabricated release catalog or release-worker fence.
The exact expected refusal is `DENIED/release_policy_denied`. None of these
probes requests release-worker activation, arbitrary commands or Docker control.

Before sending, the client independently opens the management admission
SQLite database in `mode=ro` and verifies that the original key is absent.
It then fsyncs a normal fixture original-send ticket. Only the exact producer
policy denial, no returned operation ID and a second independent empty-key
lookup permit a terminal `REFUSED` ticket. The report includes a digest of
that nonsecret absence witness. An unavailable database, unexpected response,
lost response or contradictory admission leaves the ticket held; the client
never allocates a replacement key. If the producer unexpectedly admits the
request, the original operation ID is persisted and the scenario fails for
operator inspection. It is never retried to obtain an expected rejection.
An operation ID attached to `BUSY`, `CONFLICT` or an unbound response can name
another intent; it is never adopted as this probe's original admission.
The source-promotion journal uses a canonical source lane, so absence checking
also checks the immutable physical `target_id` parameter instead of confusing
that lane name with the selected release target. Durable recovery ledgers are
consulted before a probe can create a new ticket; clearing checkpoint fields
does not reset the matching action, target and source budget.

For example, inside the already authorized disposable management container:

```sh
python -m scripts.dot_mcp_negative_acceptance \
  --fixture-manifest /fixture/fixture.json \
  --checkpoint /fixture/management/operator-refusal-checkpoint.json \
  --operator-stage observe \
  --operator-stage-revision ORIGINAL_OPERATOR_REVISION \
  --scenario observe-refusal --enable-refusal-probe \
  --fixture-issue-number ACTUAL_TEST_ISSUE_NUMBER
```

The placeholders must be replaced with original operator-selected values.
For downgrade testing, retain the positive client's successful
`dot-fixture-reevaluate-original-send.json`; after the operator changes current
policy to `observe`, select `policy-downgrade` with a separate original probe
key. A known completed reevaluation is not an unresolved-operation retry.

After genuine beta acceptance, select `--scenario beta-release-refusal
--enable-refusal-probe --operator-stage beta` with its original beta-stage
revision and separate fixed probe key. The checkpoint must preserve the real
tested-source CI/ref identity and beta acceptance ref/OID. The Issue-number
argument is not used by this source-promotion probe.

## Original-operation and recovery protocol

Read-only scenarios require `pending_operation_id` and its original
`SENT_UNKNOWN` or `NEEDS_ATTENTION` checkpoint state, uniquely bound to the
saved beta or release operation field. They request only
`auto_coder_operation_inspect`; they never call the effectful
`auto_coder_operation_get`. Inspecting the original operation precedes any
worker health dependency, so a dead child does not suppress journal evidence.

An unknown source promotion must use the saved `promotion_operation_id` and
the fixture release target; its real `selected_source_sha` must equal the
original source. An unknown beta acceptance must use the fixture beta target.
Its non-dispatching MCP projection does not expose source parameters, so the
client additionally checks the original SQLite intent in `mode=ro` for the
exact action, beta workload, source and fixed idempotency key. A separate
negative checkpoint may bind that acceptance ID as its selected
`beta_operation_id`; preserve the original deployment checkpoint and ticket
unchanged. Publication operations are accepted only by `unknown-effect`, not
as held drains or worker recovery attempts.

The repeated-failure scenario records the actual producer's consumed attempt
in `dot-recovery-<failure-signature>.json`, where the signature hashes the
immutable action, target, source SHA and producer failure category. The file
retains the original operation and is created exclusively and fsynced. Repeated
reads preserve its bytes. Another operation cannot replace that failure's
original proof. This companion never consumes a new recovery attempt or
circumvents the producer's normal retry, slot, exhaustion or merge gates.
The real prior-runtime restoration projects `status=FAILED` for the requested
candidate deployment with `recovery_state=CONFIRMED`; restoration does not
convert the failed candidate into a successful deployment or reset its budget.

For `data-rollback`, the explicit unsupported outcome is the safety contract.
There is no instruction to implement persistent-data rollback, manufacture an
incompatibility oracle or turn an operator assertion into producer evidence.
An operator must assess restoration and compatibility outside the runtime
rollout loop before resuming admission.

## External physical fault prerequisites

Fresh OAuth consent and the beta canary repository do not create recovery
evidence. The external test orchestrator must provision the following before
running the corresponding scenario. These are disposable bootstrap and fault
insertion steps; they never grant Docker or policy authority to this client.

| Physical scenario | External setup and actual oracle |
| --- | --- |
| Refusal and downgrade | Start a real installed `process-issues` worker with the scoped workload read credential, enabled idle backend, empty Issue/PR author allowlists and advancing producer heartbeat. Exercise observe refusal, then successful beta reevaluation, then an operator-owned downgrade to observe with a separate denial key. |
| Beta-only release refusal | First obtain actual exact-source CI/ref proof and published beta acceptance. Keep the operator stage and current policy at beta, preserve that original evidence and run only the fixed denial probe. |
| Held drain | In a fresh fixture, independently install and start a prior Python runtime. Enable `AUTO_CODER_TEST_HOLD_INSTALLED_TERMINAL=1`, place its exact environment path in `fixture-hold-selected-runtime`, and create `fixture-hold-terminal-exit` under the worker runtime root. The existing wrapper holds the real OS lifetime after the production worker writes its terminal receipt. Admit one candidate source update and verify original `DRAINING`, same live incumbent, the original drain step and no start step. Record before its finite lifecycle deadline expires. |
| Durable unknown | Retain the admitted original ID and key before externally interrupting the client/manager or losing a bridge response. Preserve the original journal, Supervisor and worker volumes. Inspect that same operation without `operation_get`; compare the real request/launch audit before and after reconstruction. A missing original admission ID remains held and is never repaired by another send. |
| One prior-runtime recovery and exhausted budget | Start a genuinely installed prior runtime with verified binding and advancing heartbeat, then admit one candidate update in a separate fixture. Enable `AUTO_CODER_FIXTURE_FAIL_STAGED_ONCE=1` and create `fixture-crash-next-staged` before the candidate starts. The real child exits, the Supervisor proves that OS lifetime ended, and the original lifecycle may attempt one `RECOVER_PREVIOUS`. Require actual prior-runtime binding and advancing heartbeat for `FAILED` plus `recovery_state=CONFIRMED`, or retain `NEEDS_ATTENTION` if restoration is ambiguous. Run `repeated-failure`, preserve its durable signature and original journal, and confirm later invocations cannot send a new attempt. |
| Unsupported data rollback | Use that genuine failed/restored original runtime operation. Run `data-rollback`; retain `BLOCKED`, suspend admission and escalate compatibility assessment to the operator. Do not alter a database or fabricate a compatibility receipt to manufacture this test result. |

Prior and candidate installations must come from pinned real source revisions
through `WorkerVersionPreparer` and the production Supervisor; copying a READY
file is not preparation. The existing staging/installed-worker integration
fixtures show this bootstrap pattern. Each fault fixture retains its own source
identity, targets and original records. The one-shot positive harness does not
provide a second `beta-source` admission after its first successful ticket;
prepare the prior runtime externally before the candidate's first admission.
Do not delete tickets, reset a held lane or change keys to reuse an ambiguous
fixture for another scenario. Release terminal holds only as an explicit
external test action after recording the negative boundary.

## Verification provenance

`tests/test_dot_mcp_negative_acceptance.py` is an **offline** regression suite.
Its refusal/downgrade cases use actual HTTP policy, grant and SQLite code with
the existing controlled Firebase network-edge fixture. Lifecycle-consumer
cases use the actual lifecycle projection over deterministic typed inputs.
These tests prove consumer behavior and regression boundaries, not a live
OAuth session, a real installed worker, a physical drain or a GitHub action.
The externally provisioned disposable scenarios above must be run separately
with their original evidence; absent credentials or physical fixtures remain
`BLOCKED`/`NOT_RUN`.
