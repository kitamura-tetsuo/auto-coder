# Authenticated read-only MCP target discovery

The management MCP resource registers `auto_coder_targets` with no arguments.
It needs the current `auto-coder.read` OAuth grant and is read-only, idempotent,
non-destructive and closed-world. The existing HTTP authentication boundary
rejects invalid, revoked or expired access tokens (401 `invalid_token`),
insufficient scopes (403 `insufficient_scope`), denied operator permission
(403 `access_denied`), and unavailable authentication authority (503
`temporarily_unavailable`). No target list is produced on those refusals.
Any supplied tool argument is rejected as `invalid_request`.

On success, the structured MCP tool result is:

```json
{
  "status": "OK",
  "reason": "inspectable_targets",
  "targets": [
    {"target_id": "north.beta", "repository": "fixture/beta", "channel": "beta"}
  ]
}
```

The array is ordered by logical `target_id`, with exactly those three fields
per entry. These IDs come from the current operator-owned policy and fixed
worker-client configuration, not from channel names, instance IDs or a registry
created by discovery. Each invocation loads and validates the policy afresh,
then applies the existing exact-client matching and `INSPECT` policy decision.
A configured target is visible even when its worker is stopped or unreachable;
discovery makes **no** Supervisor calls and does not prove worker health,
installed identity, deployment readiness or mutation permission. An `observe`
policy can permit inspection without permitting worker mutations.

A valid policy with no eligible aliases returns `OK` with `targets: []`.
Missing, malformed, duplicate-key or unavailable policy input, or a needed
unconfigured fixed bridge, returns an `UNAVAILABLE` status with a stable
non-secret `reason` and **no** target list. A partial list is not returned
if any candidate cannot be classified. Discovery does not expose endpoint
addresses, ports, credentials, raw policy or exception detail.

Use the returned `target_id` with `auto_coder_status` or `auto_coder_logs`
and the same authorized read-only grant. Every later call rechecks the current
authorization, target policy and fixed bridge; old discovery results confer
no lasting authority. Neither discovery nor a later status/log request can
authorize lifecycle or source deployment effects.
