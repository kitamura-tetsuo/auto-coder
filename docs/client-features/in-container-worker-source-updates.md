# Authenticated in-container worker lifecycle and beta source updates

The independent management MCP service controls existing beta/release
Supervisor child processes without calling Docker or `docker compose`, changing
images, modifying live site-packages, or accessing a Docker socket. Each worker
retains its channel identity and persistent mounts. The image-installed PID-1
Supervisor cannot replace itself. Bootstrap, native/system library upgrades,
and Python interpreter changes remain manual operator duties.

## Operator-provisioned initial profile

The supported profile is a Linux worker container with Python 3.12,
`/usr/local/bin/uv` pinned to 0.7.22, a trusted Git checkout at
`/workspace/source`, independent `/runtime/worker-versions` and the existing
authenticated per-container private Supervisor bridge. The source repository
(`kitamura-tetsuo/auto-coder`) is distinct from the per-channel work
repository. Set `source_repository` and an absolute
`source_evidence_token_file` in the management YAML. The example deployment
uses `/run/secrets/source_evidence_read` mounted from the host file selected
by `AUTO_CODER_SOURCE_EVIDENCE_TOKEN_FILE`. The credential needs GitHub
repository/Actions read permission, not Git mutation permission. It never
appears in MCP arguments, worker requests, journal snapshots or logs.

## Tools and authorization

`auto_coder_deployment_status` and `auto_coder_operation_get` require
`auto-coder.read`. Fixed `auto_coder_start_worker`, `auto_coder_stop_worker`,
`auto_coder_restart_worker` and `auto_coder_deploy_beta_source` additionally
require `auto-coder.deploy` with active original consent. At admission and
immediately before each new effect, the management service checks the original
grant, current UID/client policy, and the trusted `observe`/`beta`/`full`
operating profile. `observe` permits only reads; `beta` permits beta changes;
`full` permits beta/release process controls and **beta-only** source updates.
Release revision deployment is a separate contract. Requests contain only
target alias, bounded idempotency key, observed Supervisor/worker/runtime
generation, and for beta update a full exact Git SHA. They do not expose a
command, executable, Docker target, repository URL, path, shell, credential,
force-kill or release code update.

## Exact source and current-selection evidence

The immutable `auto-coder-tested-<SHA>` annotated tag must point directly to
the requested commit and contain canonical `source-required-v1` proof from
its **original** successful `Test Source for Runtime` execution. The manager
validates that immutable object and the original run/attempt's required jobs.
A later successful test execution of the *same* SHA may separately become the
current beta selection without altering that original proof.

Both `refs/tags/auto-coder-beta-current` (direct commit pointer) and
`refs/tags/auto-coder-beta-selection-current` (pointer to the **immutable
annotated history tag object**) must agree with the canonical
`auto-coder-beta-selection-r<RUN_ID>` record and its exact selected successful
source-test run/attempt, required successful job IDs/completion times and
maximum required-job completion time. The second pointer distinguishes two
beta-selection operations choosing the same SHA. The trusted selector
publishes it only after confirming the source pointer. An older deployment
with only a bare beta SHA is not source-eligible until the trusted
`Advance Beta Source` workflow establishes and reads back the selection ref.

The manager stores both identities and the bounded original proof/selected
execution job observations in the management SQLite operation journal.
Same-key replay recovers that original evidence before any fresh source
lookup. After staging, but immediately before the first replacement effect,
the manager revalidates the exact accepted source and selection. A changed
proof or same-SHA selection supersedes *unsent* replacement; a successful
unselected newer CI run alone does not. Once a replacement may have been
sent, only its original effect receipt and binding may be reconciled.

## Staging, graceful process control and outcomes

One physical instance has one persistent management-operation lane. Every
stage/stop/start/recovery dispatch records a stable effect ID and
`SENT_UNKNOWN` before its first request; lost replies are reconciled with
read-only queries for exactly that ID. Stages include `PREPARING`, `DRAINING`,
`STARTING`, `RECONCILING`, `SUCCEEDED`, `FAILED`, `SUPERSEDED`, `RESTORING`,
`RECONCILING_PRIOR`, and `NEEDS_ATTENTION`. Unknown results and finite
deadlines hold their lane and do not claim success.

The worker-container preparer performs a separate pinned locked-`uv` install
for the exact source and independently verifies its immutable `READY.json`;
failure leaves the incumbent and selected pointer alone. Real graceful stop
requires matching worker terminal receipt and original OS child exit, not
just a command acknowledgement. An unsettled invocation is allowed to keep
draining; no deadline sends SIGKILL, forced second SIGINT, or a container
restart. A subsequent start is generation-fenced and success additionally
requires the live new worker's matching installed source/version/lock identity,
completed startup reconciliation, daemon-serving, open invocation gate and
at least two increasing genuine `producer:` heartbeat observations. Neither
CI PASS, READY, PID existence nor Supervisor liveness is sufficient.

On verified new-beta-child crash after actual incumbent exit, at most one
newly authorized `RECOVER_PREVIOUS` effect may restore the immediately prior
independently verified prepared runtime. A restored old worker is reported as
recovery of a **FAILED** deployment, never new-deployment success. Unknown
recovery remains `NEEDS_ATTENTION`. Python runtime restoration never undoes
persistent data, migrations, GitHub effects or remote provider actions.

Unsupported host-native dependencies, image/Compose replacement, automatic
Supervisor upgrades and release source activation are intentionally absent.
