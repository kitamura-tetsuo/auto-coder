# Management-service self-update authorization

`auto_coder.management.self_update_policy` is the **advisory decision producer** for
the management service updating its own replaceable child runtime. It is not an
MCP endpoint, scheduler, source-eligibility check, dispatcher, process controller,
or bootstrap updater. A positive decision cannot by itself admit or execute an
operation. Production consumers must also enforce registered MCP tool scope
admission, durable operation ownership, source/candidate correctness, and the
final effect boundary. This feature does not change worker Issue/PR processing.

## Trusted operator opt-in

The same operator-owned, locally mounted YAML file consumed by
`load_operating_policy` supports one optional, distinct top-level key:

```yaml
profile: full
targets:
  beta-worker:
    channel: beta
    repositories: [example/widgets]
  release-worker:
    channel: release
    repositories: [example/widgets]
management_self_update:
  enabled: true
  instance_id: manager-main
  repository: kitamura-tetsuo/auto-coder
```

`management_self_update` is **absent by default**, meaning disabled. If present,
it must contain exactly `enabled` (YAML Boolean), `instance_id` (nonempty, at
most 128 characters) and `repository` (exactly
`kitamura-tetsuo/auto-coder`). `enabled: false` denies self-updates even in
`full` mode. A string such as `enabled: "true"`, a wrong repository, duplicate
keys, or malformed/unreadable configuration is unavailable, never an opt-in.
Neither `full` alone nor a worker's `beta`/`release` target mapping permits
manager updates. The canonical operator policy revision incorporates the
management setting when present; existing files retain their prior revisions
and worker evaluation matrix when the setting is absent.

The separate **trusted read-only bootstrap configuration** must identify the
same `instance_id`. `BootstrapConfig.read()` is the authoritative local instance
producer. Management-update requests are restricted to action
`management-service-self-update` and the fixed repository above; a caller
claiming another instance, repository, worker channel, release promotion,
rollback, or executable action cannot supply the missing authority. Operator
configuration and bootstrap paths must be selected by the controller, never
supplied by MCP arguments, a GitHub Issue/PR, CI status, model output, or a
service-controlled edit.

## Original consent and effect boundary

The user must explicitly consent to both `auto-coder.read` and
`auto-coder.deploy`. As described in `management-action-scopes.md`, those
scopes must be enabled on the OAuth service and the bound client. A fresh
authenticated bearer is checked through the actual `OAuthGrantService` on
admission; a Firebase UID alone, `auto-coder.operate`, or a request-supplied
grant ID does not qualify. A successful admission returns a typed
`MANAGER_UPDATE_ALLOWED` decision and the original non-secret
`AcceptedManagementUpdate`; this is **not** an effect permit.

The trusted caller can persist the result as one immutable
`ManagementOperationStore` intent with a distinct `management` journal channel,
exact management action/instance/repository, selected source SHA, canonical
admission policy revision, and the original actor/client/grant/scope bindings.
`journal_entry_for_update` constructs the operation and authority records but
neither persists them nor verifies the source SHA's eligibility. The future
dispatcher must persist these records before beginning any effect.

Immediately before the first still-`NOT_SENT` forward effect, the consumer must
call `recheck_management_update_before_effect` against the original journal
operation ID. It rereads current full+opt-in policy and trusted bootstrap
identity, requires the **same canonical policy revision**, and invokes the
original grant's real `check_pending_authority` with read+deploy. Grant
revocation, expiry, scope loss, operator UID removal, profile downgrade, config
unavailability, or a changed policy revision blocks the unsent effect. A
refreshed or different bearer cannot rescue the original grant.
The returned `journal_revision` must be used as the expected revision for an
atomic journal `begin_effect` reservation; changing the journal between the
authority check and that reservation is a conflict, not an extra send permit.

For an already possibly sent journal effect, the producer never returns
forward permission again. It can classify that **exact journal operation** as
`EXACT_RECEIPT_OBSERVATION_ONLY`; the later dispatcher may only observe the
previous receipt and perform separately specified, pre-recorded bounded
same-effect safety recovery. That classification cannot select a new SHA,
generate a new effect/operation ID, repeat start/stop, or authorize rollback
from a caller-supplied flag. Journal or policy uncertainty never makes a new
forward effect eligible. This adapter itself performs no reconciliation or
recovery action.

## Fail-closed rollout and rollback checklist

1. Keep `management_self_update` absent or `enabled: false` while validating
   normal worker behavior and operator-only `observe`/`beta` profiles.
2. On a read-only controller mount, verify the intended management bootstrap
   instance ID, fixed repository, OAuth client's explicit read+deploy consent,
   current operator UID policy, and canonical revision.
3. Deliberately select `profile: full` **and** `enabled: true`; record revision
   and test a fresh decision. Verify journal persistence and the future
   dispatcher/source correctness checks independently before attaching effects.
4. To stop **new or unsent** self-updates, set `enabled: false` (or downgrade to
   `observe`) locally and verify fresh refusal. Revoke the original grant when
   required. Do not erase the journal or assume an already-sent process effect
   was rolled back; inspect and reconcile that exact operation's receipt under
   the consumer's bounded recovery contract.

No permission is granted for modifying the immutable bootstrap, controlling
Docker Engine/socket/CLI, changing operator-owned policy through MCP, executing
arbitrary commands, exporting credentials, updating a worker, promoting release,
writing arbitrary host files, or autonomously restarting without limit.
