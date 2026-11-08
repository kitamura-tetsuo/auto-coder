# Independently managed workers on shared beta/release channels

Management selects every worker operation by an operator-provisioned target ID,
not by its release/beta channel. The channel determines update policy; multiple
distinct beta Supervisors may run the same tested Auto-Coder SHA without
sharing an endpoint, worker runtime, credential, operation lane or source
selection. MCP callers cannot supply a repository, network address or key.

## Trusted registry schema

`AUTO_CODER_WORKER_REGISTRY` contains a JSON object with a nonempty `workers`
array. Every entry has exactly these fields:

| Field | Meaning |
| --- | --- |
| `target_id` | Unique MCP target name, e.g. `beta-agent-table-worker` |
| `supervisor_instance_id` | Unique authenticated Supervisor instance |
| `channel` | `beta` or `release` |
| `repository` | One unique `owner/name` development repository |
| `bridge_address` | Fixed private, non-loopback Docker IPv4 address |
| `bridge_port` | JSON integer from 1024 to 65535 |
| `bridge_secret_file` | Absolute path to that worker's machine-secret file |

The validator rejects duplicate JSON keys, target IDs, Supervisor IDs,
address/port pairs, repositories, secret paths and secret *contents*, as well
as incomplete entries and invalid types. Machine secret bytes are read from
separate trusted files and never appear in the registry or MCP API. All
entries must validate before management enables any bridge. An absent
registry allows the identity-only service; partially specified registries do
not silently enable a subset. The legacy channel-specific bridge variables
are rejected, not loaded through a parallel endpoint-resolution path.

The maintained production examples are `compose.management.yml` (registry)
and `compose.channels.yml` (three isolated Supervisors). Provision this
read-only operating policy for the specified topology:

```yaml
profile: full
targets:
  release-worker:
    channel: release
    repositories: [kitamura-tetsuo/auto-coder]
  beta-worker:
    channel: beta
    repositories: [kitamura-tetsuo/outliner]
  beta-agent-table-worker:
    channel: beta
    repositories: [kitamura-tetsuo/agent-table]
```

Use `profile: beta` if release mutation is not authorized. Each policy target
must exactly match one registry target's repository/channel; duplicate
active repository ownership is rejected by the registry. Unknown targets and mismatched
policies are denied without connecting to another worker. OAuth scopes,
operating policy checks, graceful-stop restrictions and generation fences are
unchanged.

## Migration from the two-channel deployment

1. Pause new management mutations and back up the SQLite journal and trusted
   bridge configuration. Keep the original release-worker and beta-worker
   bridge endpoints, machine keys, Supervisor state and operation ledgers
   unchanged during migration. Record all unfinished operations. Do not
   rotate an old endpoint or secret while legacy operations remain.
2. Provision a distinct Agent Table development checkout beneath the worker's
   own `./runtime/workspaces/beta-agent-table` workspace and retain a separate
   trusted Auto-Coder staging checkout at that worker's `/workspace/source`.
   Use runtime/state/log root `./runtime/beta-agent-table` and independent HOME/authentication and XDG
   config/cache storage inside that runtime. Generate a third machine secret
   and reserve another private bridge IP. Set the
   `BETA_AGENT_TABLE_WORKER_BRIDGE_ADDRESS`, `_PORT` and `_SECRET_FILE`
   variables. Set `BETA_AGENT_TABLE_DIGEST` independently; it can equal the
   existing `BETA_DIGEST` without becoming a shared version selector.
3. Replace the manager's ten `AUTO_CODER_{BETA,RELEASE}_*` settings with the
   JSON registry in `compose.management.yml`. Retain the original target IDs
   and exact existing bindings, append `beta-agent-table-worker`, and install
   the three-target policy above. Validate with `docker compose -f
   compose.channels.yml config` and `docker compose -f compose.management.yml
   config` before applying the changes on the host.
4. Restart management. Its bootstrap preserves the entire validated registry
   in each replacement child environment. The journal pins pre-registry
   operation IDs to the original migration-time bridge digest on their first
   access, for safe reconciliation after later manager restarts. Retain the
   `legacy_worker_bindings` SQLite table and original Supervisor ledgers. If
   legacy identity or endpoint history cannot be proven, hold the operation
   for manual reconciliation rather than redirecting effects.
5. Inspect all three exact target IDs after startup and verify their signed
   Supervisor generation, instance ID, repository and worker process/run
   identity before resuming mutations. The manager neither starts Docker
   services nor changes host credentials during this process.

## Target-specific operation procedures

Use the existing MCP methods with an exact `target_id` argument, for example:

```json
{"name":"auto_coder_deployment_status","arguments":{"target_id":"beta-agent-table-worker"}}
```

The same selection applies to status, logs, consumer identity, validation
attempts, PR diagnostics, operation reads, reevaluation, start/stop/restart,
beta source deployment and beta runtime acceptance. For mutation, inspect
the original worker first, pass its exact run/supervisor/deployment/runtime
fences, and use a client idempotency key. A key for another beta target has a
different journal namespace. Retained results include the target identity.

Each new worker operation stores a non-secret `worker_binding` digest covering
the fixed target/instance/channel/repository, bridge address and port,
credential file identity and machine-key fingerprint. Reads and effects for a
pending operation require that original binding. A changed binding cannot
receive its pending operation or replay its first target's result; missing
receipts remain `SENT_UNKNOWN` until read from the original ledger. Old
two-channel operations are allowed to reconcile only with their original
identities and the persisted migration-time pin.

One unreachable beta bridge reports its own `UNAVAILABLE` status. The other
beta and release clients, locks and journal lanes remain independently
inspectable and controllable. See the production-to-dashboard boundary in
[`docs/dashboard-observability.md`](../dashboard-observability.md).
