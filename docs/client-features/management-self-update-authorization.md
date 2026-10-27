# Management self-update authorization

`auto_coder.management.self_update_policy` provides an independent **advisory**
management-service self-update decision. It does not register a management MCP
tool, admit an operation to the journal, launch an update, alter the Supervisor,
or cause a restart. The later trusted dispatcher must combine its decision with
the original request admission, durable operation ownership, version staging,
and the actual effect-safety checks before dispatch.

## Operator-owned opt-in

The operating policy YAML selected by the operator accepts the following
additional top-level entry. The normal `targets` beta/release declarations must
still be present and are left unchanged.

```yaml
profile: full
targets:
  beta-service: {channel: beta, repositories: [example/widgets]}
  release-service: {channel: release, repositories: [example/widgets]}
management_self_update:
  instance_id: production-manager
  repository: kitamura-tetsuo/auto-coder
  enabled: true
```

The independently operator-owned management service configuration (`--config`)
must also identify this same stable running instance:

```yaml
management_instance_id: production-manager
```

That line is added to the existing management configuration; it is not a
standalone valid management configuration. `management_instance_id` is omitted
by default, and the `management_self_update` policy block is omitted by
default. Within a configured block `enabled` defaults to `false`; only the
literal boolean `true` opts in. Explicit `full` **alone never opts in**.
Either omitted instance ID, missing policy block, wrong instance, wrong/fixed
repository, invalid YAML, unreadable configuration, or policy revision change
before the effect refuses the operation. Configuration is read from operator
paths at every evaluation, with no MCP, Issue, PR, CI, or model-controlled API
for enabling the feature.

The management action identity is exactly `management-service-self-update`
with journal channel `management`, the operator-configured stable instance ID,
and fixed repository `kitamura-tetsuo/auto-coder`. It is not worker-control,
worker-deployment, release promotion, or bootstrap Supervisor code update.
Other management service instances and repositories cannot use this grant.

## Original OAuth consent and effect boundary

For a *new request*, the adapter calls the real `OAuthGrantService` bearer
authentication, then captures its original `AcceptedAuthority`. It requires
both `auto-coder.read` and `auto-coder.deploy` in the accepted token/grant and
in currently enabled service and client policy. `auto-coder.operate`, a Firebase
UID alone, grant IDs supplied in tool arguments, or text from agents do not
satisfy this check. The original grant must also remain eligible under the
current operator identity policy. The result is `ALLOWED`, `DENIED`, or
`UNAVAILABLE` with a stable reason and canonical policy revision; `ALLOWED`
is **not** itself effect permission or a runnable update.

An integrating consumer can persist the returned non-secret accepted context
through `journal_values_for_management_update`. This uses the real management
operation journal's original actor/client/grant/scopes, exact management target
and action, accepted policy revision, and immutable expected artifact. The
consumer must store only the result of genuine bearer admission, not accept
these fields from an MCP request or a new bearer presented after queuing.
Before the **first** forward effect, it calls
`recheck_management_update_operation` with the durable operation ID and its
trusted journal. That function rereads both operator files, compares the exact
accepted revision and target, and uses `check_pending_authority` against the
original OAuth grant, including current service/client scope configuration and
operator UID eligibility. A revoked/expired or narrowed original grant cannot
be replaced with another token or grant for the queued operation.

The journal's `NOT_SENT` state is the only candidate for a forward effect.
`SENT_UNKNOWN` is conservatively possibly sent even if no response was
received, so it must never be sent again on the basis of a new policy decision.
For actual sent/possibly-sent records the adapter returns at most an exact
read-only receipt observation key (`operation_id`, `attempt_id`, correlation,
accepted artifact), even after policy or consent loss. It never grants a new
candidate, operation, SHA, restart, send, release promotion, or caller-requested
rollback; any bounded same-effect safety recovery is reserved for a future
consumer with independently validated durable evidence.

## Rollout and rollback

1. Back up both operator files and the durable operation journal. Confirm the
   management instance ID and fixed repository against the actual service,
   the client's explicitly consented read+deploy scopes, and the tested staged
   source identity.
2. Configure `management_instance_id` and the matching policy block with
   `enabled: false` first. Confirm that the worker beta/release decisions stay
   unchanged and manager updates return `DENIED`.
3. Deliberately change `profile` to `full` and `enabled` to `true`, record the
   canonical policy revision, and verify a fresh real bearer admission and
   pre-effect check. Do not interpret a green policy decision as a Supervisor
   update result.
4. To halt new effects, set `enabled: false` or downgrade to `observe`. All
   still-unsent operations will then fail their fresh pre-effect check. Keep
   the journal and immutable effect receipts for exact read-only reconciliation
   of possibly-sent operations. Do not requeue or repeat them automatically.

This opt-in grants no Docker Engine/socket/CLI access, arbitrary executable or
filesystem mutation, credential export, bootstrap Supervisor replacement,
worker-control permission, or unrestricted autonomous self-restart.
