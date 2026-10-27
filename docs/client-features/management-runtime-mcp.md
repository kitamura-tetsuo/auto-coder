# Authenticated runtime diagnostics and single-target reevaluation

The independent `auto-coder management serve --config PATH` process exposes
fixed MCP diagnostics and reevaluation tools beside `auto_coder_whoami`. The
separately opted-in management self-update tool is documented in
`management-mcp-self-update.md`. This process does **not** run
an `AutomationEngine`, execute GitHub/provider jobs, control Docker, start or
stop containers, or schedule a new retry system. Only the existing worker UDS
producer can accept a single Issue/PR reevaluation.

## Trusted deployment configuration

The management configuration adds two absolute paths:

```yaml
state_root: /management-oauth
operation_state_root: /management-operations
operating_policy_path: /management-policy/operating-policy.yaml
```

The remaining `public_origin`, Firebase project, bind and statically registered
OAuth-client fields follow `management-mcp-authentication.md`. The two new paths
are optional for deployments that use only `auto_coder_whoami`; runtime tools
then return `UNAVAILABLE` and never grant mutation authority. Operators should
provision `operating_policy_path` **read-only** from the controller, not inside a
worker filesystem, workspace, or manager-writable operation store. An absent,
malformed, duplicate-key or unreadable policy is not interpreted as `beta`.

`compose.management.yml` mounts `./runtime/management/policy` read-only at
`/management-policy` and the separate durable operations directory at
`/management-operations`. The operator must create the actual policy file and
configure its path before enabling runtime controls. For example:

```yaml
profile: beta
targets:
  beta-worker:
    channel: beta
    repositories: [example/beta-repo]
  release-worker:
    channel: release
    repositories: [example/release-repo]
```

Use the exact `RELEASE_REPOSITORY` and `BETA_REPOSITORY` provisioned in Compose,
not the example names. Each logical target must name **one** configured channel
and its **one** exact repository. The physical instance IDs, endpoint addresses,
ports and independent machine credentials come only from validated
`AUTO_CODER_{BETA,RELEASE}_*` operator environment settings specified in
`management-worker-private-network.md`; no MCP argument selects them. Both
Supervisor bridges must be provisioned together on the private Docker network.
The OAuth access bearer is never forwarded to a Supervisor. The policy's
`observe`/`beta`/`full` profile is reread for each new action and immediately
before the first unsent worker effect. A later downgrade does not authorize a
new effect, and it does not erase a previously committed receipt.

## Fixed MCP tool schemas

| Tool | Required scopes | Arguments | Meaning |
| --- | --- | --- | --- |
| `auto_coder_status` | `auto-coder.read` | `target_id`, optional `limit` (1–500) | Bounded `/api/status` plus nonce-bound worker lifecycle, heartbeat and installed runtime evidence |
| `auto_coder_logs` | `auto-coder.read` | `target_id`, optional `limit`, `item_type`, `item_number`, `execution_id`, `process_run_id`, `after_sequence`, `cursor` | Existing source-redacted, bounded log projection and retention-gap outcomes |
| `auto_coder_validation_attempt` | `auto-coder.read` | `target_id`, required positive `pr_number`, and exactly one of `attempt_id` or `attempt_sequence` | Existing exact validation-attempt projection and no-match/unavailable outcomes |
| `auto_coder_operation_get` | `auto-coder.read` | `operation_id`, plus `target_id` for worker operations | Non-secret original actor/client, immutable intent, effect state and verified receipt; exact read-only reconciliation is allowed |
| `auto_coder_reevaluate_target` | `auto-coder.read` and `auto-coder.operate` | `target_id`, `target_type` (`issue`/`pr`), positive `number`, `idempotency_key` (1–128 characters), `expected_process_run_id`, `expected_deployment_generation` | One journaled reevaluation request for the current live worker incarnation |

The MCP tool schema rejects unknown arguments. The service also rejects Boolean
or nonpositive item numbers, invalid identity/key characters and out-of-range
limits. It accepts no `force`, `retry`, `resume`, `emergency`, `backend`, model,
permission, command, arbitrary URL, path, repository, channel or machine key.
`auto_coder_reevaluate_target` is an explicitly mutating, idempotent tool;
inspection tools are read-only. Tool annotations do not confer authorization.
The normal OAuth resource authentication still yields HTTP **401** for invalid
credentials, **403** for missing original scope/current permission, and **503**
when the grant authority is unavailable *before any MCP tool executes*.

## What is authoritative

The signed bridge query binds one fresh nonce to the actual UDS producer's
repository, channel, process run, deployment generation, lifecycle,
startup reconciliation, daemon-serving state, invocation gate and heartbeat.
The selected installed runtime binding is `VERIFIED` only with the exact
Supervisor-selected runtime ID, READY content identity and locked-input digest,
joined to the same worker incarnation and the worker's actually installed
source revision/distribution version. A bootstrap worker without verified READY
evidence is reported as `UNAVAILABLE` for installed-runtime readiness;
matching source SHAs alone are insufficient. A responsive Supervisor with a
dead child is never a healthy worker observation. Diagnostics always come from
the worker's existing source-projected, redacted and bounded UDS routes; the
manager does not read worker files or synthesize missing history.

`auto_coder_reevaluate_target` first checks the current `RUNNING`, reconciled,
daemon-serving and invocation-open facts, and the exact expected run/generation.
Before the first effect it checks the **original**, durable bearer acceptance
against the current unexpired grant, original consent, UID eligibility, current
service/client scope configuration, target map and operating profile. A later
token refresh cannot expand that authority. Access-token expiry alone does not
cancel an already admitted operation, while grant expiry, revocation, removed
consent, missing operator policy or a profile downgrade prevent the new effect.
The operation journal (`management-operation-journal.md`) commits intent and
non-secret authenticated authority before the worker request. One dispatcher
owns registered handlers and per-target journal exclusion; unknown actions
remain `NEEDS_ATTENTION`. Its queue is only a delivery mechanism for already
accepted management operations, never an alternate Issue/PR scheduler.

The response `accepted_for_reevaluation` is returned only after validating the
actual worker's durable invalidation receipt: same operation ID, target,
channel, repository and accepting/requested worker incarnation. This proves
only an **ordinary reevaluation obligation** was accepted—not implementation,
review, merge, publication or provider completion. Identical idempotency
replay returns the original operation and original acceptance, not a new effect.
`CONFLICT` (changed request/key), `BUSY` (another live target operation),
`NOT_FOUND`, `UNAVAILABLE`, `NEEDS_ATTENTION` and `RECONCILING`/`sent_unknown`
are distinct. Lost responses, manager restarts, network failures or replaced
workers never release a possibly-sent operation's lane by timeout.

Once the journal records `sent_unknown`, its dispatcher may **only read** the
receipt for that exact operation ID from the fixed worker endpoint. It must
never forward the same action again, issue a substitute key or infer success
from a missing result. Auth/policy downgrades do not forbid this exact read-only
reconciliation. A durable confirmed receipt can finish the journal operation
after a crash without repeating the worker command.

Operators should retain the SQLite management journal and the worker's own
durable reevaluation receipts across restarts. Keep service logs in the
configured rotating Loguru file **and** stdout sink. Runtime results contain
only bounded identity, redacted diagnostic and safe-effect projections; they
are not cacheable and never include access, Firebase or machine credentials.
