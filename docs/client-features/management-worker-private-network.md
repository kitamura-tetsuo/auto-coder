# Private authenticated management-to-worker Supervisor transport

Each configured release/beta container's long-lived, image-installed
`auto_coder.worker_supervisor` may expose a **separate** version-1 HTTP listener
for one shared, independently authenticated MCP management container. This is
not a second worker, scheduler, public endpoint, or Docker client. The existing
process-local `/runtime/management.sock` remains the only source of worker
diagnostic and reevaluation evidence. The stable Supervisor verifies the Unix
peer's PID/Linux start token and the exact worker generation before and after
each forwarded request. If the worker stops or cannot answer, the bridge
returns `UNAVAILABLE`/`UNKNOWN`, never a synthetic healthy heartbeat.

## Initial operator bootstrap

1. Build/deploy the Supervisor image once. On the **host**, create a dedicated
   user-defined Docker bridge network with an operator-selected IPv4 subnet
   (`docker network create --driver bridge --subnet <private-subnet>
   <private-network>`). Choose two distinct, unused static addresses in the
   subnet for beta/release. Do not configure `internal: true` on the worker's
   sole egress network if GitHub/provider access is still needed. Existing
   Compose default network membership is retained.
2. Generate **independent** high-entropy base64 secrets for release and beta
   (for example `openssl rand -base64 48` per secret), in distinct host files
   accessible to the Compose operator. Do not reuse Firebase/OAuth access or
   refresh tokens, and never place the secret text in Compose environment
   values, Git, image layers, application logs, or MCP arguments.
3. Set `AUTO_CODER_PRIVATE_NETWORK` to the precreated network name;
   `RELEASE_WORKER_BRIDGE_ADDRESS`, `BETA_WORKER_BRIDGE_ADDRESS` to the reserved
   distinct IPv4 addresses; `RELEASE_WORKER_BRIDGE_PORT`,
   `BETA_WORKER_BRIDGE_PORT` to fixed listener ports (1024–65535); and
   `RELEASE_WORKER_BRIDGE_SECRET_FILE`, `BETA_WORKER_BRIDGE_SECRET_FILE` to
   **host paths** to the two distinct secret files. Retain the existing
   `RELEASE_REPOSITORY`, `BETA_REPOSITORY`, image and digest settings. Check
   `docker compose -f compose.channels.yml config` and
   `docker compose -f compose.management.yml config` before the one-time host
   `docker compose ... up -d` operations. The management service may be a
   separate Compose project; the external named network joins both projects.
4. Provision the fixed, trusted, channel-private Git repository at
   `/workspace/source` inside each existing worker workspace mount (not from
   an API request); the Supervisor stages only exact full commits from this
   root under `/runtime/worker-versions` with the image-pinned `uv` and
   bootstrap interpreter. Source staging is unavailable until this trusted
   checkout exists. A future staged worker activation only changes the child
   process, not the image, Compose configuration, or Supervisor.

`compose.channels.yml` sets the immutable channel/instance/repository identity,
private bind address/port, and `AUTO_CODER_BRIDGE_SECRET_FILE` backed by that
channel's Compose secret. `compose.management.yml` mounts the same *two*
secrets only in the trusted management container and supplies fixed
`AUTO_CODER_{RELEASE,BETA}_SUPERVISOR_INSTANCE_ID`, `_REPOSITORY`,
`_BRIDGE_ADDRESS`, `_BRIDGE_PORT`, `_BRIDGE_SECRET_FILE` variables to the
`WorkerNetworkClient` constructor. The worker containers receive only their
own key. No service publishes the Supervisor port on the host (`ports:` and
Docker socket mounts are absent from both worker services), and the manager's
existing loopback-host-published MCP port is unchanged. Bootstrap refuses a
partially specified, public/loopback or nonlocal bind, missing credential,
invalid channel/repository, or unconfigured trusted source root **before** it
launches a worker. It does not fall back to public binding or Docker operations.

The management bootstrap itself launches an immutable runtime with an explicit
environment. It forwards **only** the ten validated worker-endpoint metadata
variables from the operator-provided Compose environment into its replaceable
MCP child; a partial pair, identical keys, duplicated endpoints or duplicated
instance IDs is rejected. The management application's composition root retains
two fixed typed `WorkerNetworkClient` instances for authorized future MCP
action dispatchers. It does not create an unauthenticated management tool.

## Machine authentication and request correlation

The fixed API authenticates every request with a dedicated per-instance HMAC
SHA-256 key. Version `1` requires `X-AC-Version`, `X-AC-Instance`,
`X-AC-Supervisor-Generation`, `X-AC-Timestamp` (integer Unix seconds),
`X-AC-Nonce` and `X-AC-Request-Id` (fresh 32-digit lowercase hex), and
`X-AC-Signature`. The canonical signature covers protocol marker, method,
literal path/query, exact instance and Supervisor generation, timestamp,
nonce, request ID, expected worker run ID, expected worker generation, and the
SHA-256 of the exact JSON bytes in that order, joined by newline. Worker UDS
routes require the exact expected `process_run_id` and deployment generation in
`X-AC-Worker-Process-Run-Id` and
`X-AC-Worker-Deployment-Generation`; a replaced or unobserved child is
rejected before forwarding. Verification uses constant-time HMAC comparison and rejects
timestamp offsets over 60 seconds, reused nonce within the current lifetime,
invalid signatures, duplicate authentication headers, oversized bodies and
protocol mismatch *before dispatch*. A bounded 2,048-entry replay cache rejects
overflow rather than discarding live entries. Only the signed initial
`GET /v1/supervisor` may use generation `-` to discover the Supervisor's
current lifetime. All other calls require its exact discovered incarnation.

Authenticated response documents include schema version, request ID, exact
instance/channel/repository, Supervisor generation, observation time, result
code and bounded data. `X-AC-Response-Signature` signs the response hash,
request ID and generation. The manager verifies this signature and exact
identity/correlation before accepting *any* result. A network error, malformed
response, stale generation or missing upstream evidence is never treated as an
accepted operation. Responses are `Cache-Control: no-store`.

## Versioned fixed routes

| Method and path | Authoritative source / meaning |
| --- | --- |
| `GET /v1/supervisor` | Live Supervisor incarnation and carefully projected worker state, independent of worker readiness |
| `GET /v1/worker-observation?nonce=...` | Actual PID-verified worker UDS observation with the **same** nonce; staged runtime READY/content/lock binding is verified from that exact response, or unavailable/conflict |
| `GET /v1/diagnostics/{status,logs,validation-attempts}` | Existing bounded, redacted `/api/*` worker UDS projections with closed query fields, original status/error semantics |
| `POST /v1/reevaluations` and `GET /v1/reevaluations/{operation_id}` | Exact single-target, generation-fenced worker UDS invalidation producer and original durable acceptance receipt |
| `POST /v1/worker-version-preparations` and `GET /v1/worker-version-preparations/{operation_id}` | Exact full source SHA via the trusted #2610 preparer, durable operation ID and independently verified READY readback; never an arbitrary path/URL |
| `POST /v1/worker-operations` and `GET /v1/worker-operations/{operation_id}` | Fixed `START`, `REQUEST_GRACEFUL_STOP`, `RESTART_WORKER` via the real PID-1 Supervisor and its durable lifecycle-operation ledger |

The same closed worker-operation route also accepts `RECOVER_PREVIOUS` only
for the original, confirmed-exited successor lifetime and the immediately
previous verified prepared runtime. This is not a generic restart or arbitrary
rollback command; the manager journals its one-at-most attempt separately.

All POSTs reject unknown fields and require an immutable operation ID. Worker
mutations also require actual expected `process_run_id`, deployment generation
and existing selected runtime pointer; the currently authenticated Supervisor
generation is mandatory at the HTTP boundary. Stopped workers are represented
by `null` process identities, not an invented run ID. No route accepts a shell
command, Docker target, repository URL, executable, installation path,
environment mutation, generic proxy destination, OAuth token or worker policy.
Maximum request document: 16 KiB; maximum response: 256 KiB; at most four
concurrent connections, eight queued TCP connections and a bounded 5-second
socket observation timeout. The staging effect itself runs asynchronously in
the fixed Supervisor with `IN_PROGRESS` readback and bounded preparer commands,
without blocking the health listener.

## Lost responses, restarting managers, and stopped workers

Operation IDs remain bound to their first accepted effect. After a client
timeout, **query the same operation ID** before considering a retry, and if
re-sending use the *identical* intent and ID. The management journal—not a
client-generated replacement ID—owns effect retry policy. A `NOT_FOUND` does
not prove the previous request failed before dispatch unless the upstream
ledger is available and the same lifetime/effect boundary was checked. The
worker Supervisor's effect generation and persisted operation record fence
restarts; an interrupted preparation can be reconstructed as `READY` only
from the exact independently validated staged receipt, otherwise `UNKNOWN`.

The listener stays up if the worker drains, crashes or stops. UDS-backed
diagnostics and reevaluation are `UNAVAILABLE` when their producer is absent;
Supervisor state and stored lifecycle/READY receipts remain queryable.
Manager restart or temporary private network outage does not stop/restart a
worker and never causes a Docker/Compose invocation. Secret rotation and
listener address changes require trusted operator redeployment of both sides;
old Supervisor generations and HMAC nonces cannot authorize mutations after
replacement.

## Supported trust boundary

This profile assumes a trusted single-host private Docker bridge with
operator-selected peers and secrets. It does not claim TLS, confidentiality or
peer isolation against an untrusted container already allowed onto that
network, or cross-host/WAN authentication. Do not expose the listener to an
untrusted cross-host network. No bridge operation gains Docker Engine authority
and neither the management container nor worker container mounts `docker.sock`.
External Docker is permitted *only* for disposable integration fixtures and
initial manual operator bootstrap, never inside the running management API.
