# Authenticated management-service self-update

`auto_coder_update_management_service` updates the one locally configured management
service to an exact, originally CI-tested source revision. It uses the existing
management operation journal and dispatcher, then hands physical child replacement
to the independent immutable management Supervisor. The externally configured MCP
URL remains unchanged. The initiating HTTP response can be lost during replacement;
its survival is not part of the success contract.

## Manual prerequisites

An operator must first deploy a bootstrap version containing the self-update
handoff consumer. A plain `auto-coder management serve` process cannot supervise its
own replacement and returns `UNSUPPORTED` for this action. Upgrading the bootstrap
executable or its container image remains a separate manual deployment operation.
The MCP action never invokes Docker, Compose, a Docker socket, a host service
manager, or an installer in the serving interpreter.

Provision distinct absolute roots for bootstrap process state, retained OAuth
state, retained operation state, management source checkout, and prepared management
versions. Configure the source checkout for `kitamura-tetsuo/auto-coder`. Its exact
commit and lockfile must agree with the original successful source-test receipt
produced by `test-source-for-runtime.yml`; a green run for another commit, a moving
branch name, or an unverified local READY file is insufficient.

The management YAML adds these operator-selected paths to the existing issuer,
Firebase project, client registration, and listener settings:

```yaml
state_root: /management-oauth
operation_state_root: /management-operations
operating_policy_path: /management-policy/operating-policy.yaml
bootstrap_config_path: /management-bootstrap/config.json
self_update_source_repository: /management-source/auto-coder
self_update_python_executable: /usr/local/bin/python3.12
self_update_uv_executable: /usr/local/bin/uv
self_update_uv_version: "0.9.5"
enabled_scopes: [auto-coder.read, auto-coder.deploy]
```

Use the actual installed `uv --version` value, not the illustrative version above.
The registered client's `allowed_scopes` must also explicitly contain both scopes.
A caller must consent to both; enabling them in configuration does not upgrade an
older read-only grant. The source evidence reader uses the existing GitHub REST
client and can read this public repository without a bearer credential. GitHub
rate limits and unavailable original run evidence remain typed refusals.

The read-only bootstrap JSON retains the fields documented in
`management-service-supervisor.md` and additionally fixes:

```json
{
  "management_version_root": "/management-versions",
  "management_config": "/management-config/management.yaml",
  "verification_timeout": 120
}
```

These are fields of the full bootstrap document, not a complete configuration.
Its `oauth_root` and `journal_root` must exactly match the management YAML. Its
loopback `listener_url` must identify the same port and `/mcp` path. The public
issuer can remain HTTPS behind the existing proxy; the local protected challenge
uses that issuer's Host header while connecting only to the fixed loopback URL.

Prepare both the initial/previous manager and each candidate with the existing
`ManagementVersionStager`, using the same immutable management YAML and instance
ID. The staging contract is documented in
[`management-runtime-staging.md`](management-runtime-staging.md).
The action consumes already independently installed
`revisions/<content>/<attempt>/MANAGEMENT_READY.json` receipts; it does not install
or repair a partial environment. The initial child must itself have a verified
management READY receipt so that compatibility and the single return runtime can
be checked before stopping it. A generic unverified bootstrap executable is not a
sufficient previous runtime for self-update.

`self_update_handoff.runtime_for_receipt(bootstrap_config, verified_receipt)` builds
the trusted `management-installed` `RuntimeSpec`. It selects only the receipt's
installed interpreter and the fixed command
`python -I -B -m auto_coder.cli management serve --config <configured-file>`.
Serialize its environment as a JSON mapping and paths as strings in the bootstrap
runtime list. Set `initial_runtime` to the initial receipt's
`management_runtime_id`. Dynamic candidate profiles are retained by immutable
receipt identity in the private handoff store; MCP arguments never select a path,
command, repository, URL, credentials, container, image, or worker channel.

## Operator-only authorization

The existing operating policy must explicitly opt this management instance in:

```yaml
profile: full
targets:
  beta-worker:
    channel: beta
    repositories: [kitamura-tetsuo/auto-coder]
management_self_update:
  enabled: true
  instance_id: manager-main
  repository: kitamura-tetsuo/auto-coder
```

Use the exact bootstrap instance ID and retain the actual worker targets. Neither
`full` alone nor a beta/release worker mapping authorizes a management update.
Missing, unreadable, or malformed configuration never implies permission.

Admission records the original actor, OAuth client, grant, consent, canonical
policy revision, target, source SHA, idempotency key, and operation ID together.
The original grant and current policy are checked again after slow source/preflight
verification and immediately before the durable handoff. The Supervisor rereads
them again before its first physical stop claim. A later token cannot rescue an
accepted but unsent operation after original grant revocation, consent loss,
policy revision change, or phase downgrade.

After an exact effect may have been delivered, reconciliation can only observe
that operation and perform the single safety return already bound to its handoff.
It cannot choose another version or borrow authority from a new principal.

## Request and reconnect

Call the fixed tool with exactly two arguments:

```json
{
  "name": "auto_coder_update_management_service",
  "arguments": {
    "source_sha": "0123456789abcdef0123456789abcdef01234567",
    "idempotency_key": "manager-update-2026-10-27"
  }
}
```

Replace the illustrative SHA with the exact full lowercase 40-character source
commit. Keys contain 1–128 ASCII letters, digits, periods, underscores, colons, or
hyphens, beginning with a letter or digit. Unknown arguments are rejected.

The response immediately exposes the durably admitted `operation_id`, candidate
source SHA, phase/effect state, the unchanged reconnect URL, and the exact readback
call. Source checks and compatibility probes continue on the existing dispatcher.
`dispatch_queued: false` means its bounded queue is full: keep the original key and
query the same operation to enqueue it again. Repeated queries share an in-flight
attempt. Reconnect and use:

```json
{
  "name": "auto_coder_operation_get",
  "arguments": {"operation_id": "the-original-operation-id"}
}
```

No worker `target_id` is needed for a management operation. Existing worker queries
continue to require their own target ID. Only the original principal and OAuth
client can query this management intent. They may use a fresh valid token for
observation; that token does not replace the original mutation authority. If the
initiating reply was lost before revealing its ID, replay the original SHA/key to
recover the same record. A different SHA under the same key conflicts. Another
key cannot bypass the busy management lane.

The old child receives one cooperative graceful stop. Active authenticated HTTP
requests finish before the child writes an exact generation/nonce/PID/start-token
drain receipt. Only actual OS lifetime exit plus that receipt permits a new child.
A drain timeout leaves the operation pending; it never sends SIGKILL, repeats the
stop, or starts a competing listener.

## What proves completion

Before any stop, both installed runtimes are checked against their original
source-test, tree, lock, Python, package, profile, entrypoint, configuration, and
content identities. Disposable private SQLite online backups include committed
WAL data. Actual previous, candidate, and previous-return code must read retained
OAuth/journal state without destroying existing data or making the previous
runtime unable to read it. Live credentials and operation records are not migrated,
wiped, restored from a snapshot, or reinitialized by this check.

The replacement then progresses through separate process-running, application
startup/journal reconciliation, transport reachability, protected authentication
challenge, and authenticated readback evidence. The response's `verification`
object exposes the handoff phase, selected process observation, local startup
reconciliation, protected challenge, authenticated readback, and deadline
separately; these observations do not individually imply `SUCCEEDED`.
During the configured verification
window, reconnect as the original principal/client and call `auto_coder_whoami` or
`auto_coder_operation_get`. The response path must run in the exact selected
installed environment and OS lifetime. Ordinary whoami response fields are
unchanged. Proof records contain grant identifiers and runtime correlations,
never bearer, refresh, or Firebase tokens.

`SUCCEEDED` requires both a real protected 401 challenge at the unchanged endpoint
and a fresh valid authenticated application readback, joined to the exact current
child, original journal effect generation, and selected READY receipt. A staged
environment, TCP connection, unprotected 200, old child response, live Supervisor,
or preflight result cannot establish success. `verification_timeout` (default
120 seconds, configurable to a positive value up to 3600) starts after the selected process is
observed; request drain can take longer without a forced kill.

## Failure and recovery

| Outcome | Meaning and next step |
| --- | --- |
| HTTP 401 / 403 / 503 | Invalid credential / insufficient current scope or permission / unavailable authentication authority; no tool effect was admitted. |
| `DENIED` / `UNAVAILABLE` with `NOT_STARTED` | A durable refusal before a process effect. Correct the original authority/configuration problem and deliberately submit a new operation if needed; replay retains the old outcome. |
| `NOT_READY` / `UNSUPPORTED` | Missing or incompatible verified runtime, insufficient disk, incompatible schema, or absent supported bootstrap. The old child stays running. |
| `RECONCILING` | The exact handoff is active. Reconnect and read the same operation ID. |
| `PROCESS_RESTORED` | One exact previous executable has a genuinely observed new lifetime. The intended update failed; retained OAuth and journal data were not rolled back. |
| `UNKNOWN` / `NEEDS_ATTENTION` | Evidence is missing, corrupt, stale, or conflicting. The lane remains reserved. Inspect this operation and the matching Supervisor effect receipts before any manual recovery. |

A failed candidate launch, live initialization failure, or verification deadline
permits at most one return to the exact previous verified runtime. A still-running
failed candidate must itself drain and exit first. A missing/unreadable previous
runtime or ambiguous return remains `NEEDS_ATTENTION`; there is no crash loop.
A crash after a completed successful update does not silently revert that update.

For stuck operations, inspect the retained `management-operations.sqlite3`,
`management-child.sqlite3`, `management-self-update.sqlite3`, exact OS lifetime,
and both READY receipts using operator-controlled read-only tools. Preserve these
records and the source/configuration identities. Do not delete the journal, free
a lane because a deadline elapsed, manufacture a successful receipt, resend a
possibly delivered effect, or replace an unknown version with a different SHA.
If evidence cannot resolve the exact operation, manually stop/recover the service
under the existing deployment procedure and retain the unresolved record for
operator investigation; the MCP API supplies no arbitrary process-control escape.

## Verification and observability

The feature records bounded Loguru diagnostics in the existing console and rotating
management log sinks. The ordinary worker scheduler, reevaluation tools, beta and
release runtime state, provider traces, and dashboard schemas are unchanged.
Run the production-authenticated real-process tests, retained-state preflight tests,
and persisted-boundary Supervisor tests with:

```bash
bash scripts/test.sh tests/test_management_self_update_integration.py tests/test_management_self_update_failures.py tests/test_management_self_update_preflight.py tests/test_management_self_update_supervisor.py -m 'not browser and not opencode_live'
```

External CI/Firebase HTTP services in these tests are disposable local services.
The tests execute real source checks, installed package probes, OAuth/PKCE/grant
code, protected MCP requests, SQLite stores, and actual Supervisor child processes;
they do not contact or mutate a live operator deployment.
The two authenticated process suites carry the `supervisor_integration` marker
and run in the existing dedicated Supervisor Integration Tests workflow, alongside
its worker/process integration gates. Ordinary PR unit-test shards exclude that
marker; runtime-source eligibility still requires the existing full source gate.
