# Management proxy namespace experiment

Status: isolated experiment only; not a production cutover procedure.

The installed proxy uses Docker `network_mode: container:<manager-id>`. A
running proxy can retain the old network namespace after a manager restart;
container status and Docker restart policy do not demonstrate connectivity.

`sudo env PYTHONPATH="$PWD/src" "$PWD/.venv/bin/python" tests/manual_proxy_namespace_validation.py` creates disposable
manager and Caddy containers with unique names and a nonsecret HTTP marker. It
verifies the initial namespace and HTTP response, restarts the manager while
the proxy remains alive, and tests whether their actual `/proc` network
namespace identities diverge. A separate persistent `proxy_watch_cli` process
detects the difference and reattaches the retained proxy; the fixture checks
both namespace identity and HTTP response. It then lets a real Docker stop
complete, injects loss of that acknowledgement, reconstructs the controller,
and checks that the unknown stop is not retried. It also loses a successful
start acknowledgement and verifies that it remains unknown without replay. It starts a
successor and restarts the retained proxy against the same manager, performs a cutover to a distinct
manager and successor proxy, and rolls back to the retained pair. It removes
only its randomly named disposable containers.

On the tested host the namespace diverged after manager restart; all HTTP and
namespace checks for replacement and rollback passed. The polling watcher is
not installed as a production service. This test does not use
the production proxy config, TLS, external listener, OAuth, existing state,
operation histories, or the real management readiness endpoint. It does not
test interrupted manager cutover, host reboot or an unknown
Docker effect. It cannot authorize production cutover or rollback. A safe
production procedure must pin the exact old and new container IDs, hold the
old owner fence, stop and observe proxy and manager in order, establish absence
of the old listener, attach the intended proxy to the exact new namespace,
and independently read back an authenticated identity. An uncertain effect
must be inspected and reconciled before another action. The old manager and
proxy definitions and all state must remain available until rollback has been
proved safe.

`tests/test_management_proxy_watcher_process_integration.py` is an additional
host-root Docker test for two actual crash boundaries: it holds the Docker
command wrapper after a real stop or start succeeds, kills the watcher before
the wrapper can acknowledge, and reconstructs the watcher against the same
private journal. Both cases must retain the original unknown phase and issue
no repeated effect. The mandatory CI host selection uses the checked-out
manager image and a digest-pinned Caddy image, and refuses any skipped case.

For a disposable environment, create an owner-only `0700` state directory and
an owner-only `0600` JSON configuration containing `state_root`, exact full
`manager_id` and `proxy_id`, and their full `manager_image` and `proxy_image`
digests, plus `config_sha256` of the protected read-only Caddyfile. The watcher
refuses missing, changed, writable, shadowed or unprotected config mounts.
Start `python -m auto_coder.management.proxy_watch_cli --config
/absolute/path/to/config.json` as the trusted host owner after Docker is
available. One locked process polls; it does not start the manager or report
authenticated readiness. A new manager container ID requires a separately
reviewed proxy definition and config, not substitution of a name.
The watcher accepts only the exact direct Caddy entrypoint `caddy run --config
/etc/caddy/Caddyfile --adapter caddyfile`. The retained production proxy's
signal-gated shell is intentionally refused: restarting it does not launch
Caddy. A replacement must use the direct definition and retain the old proxy.

Before each Docker effect the watcher durably records the exact manager
lifetime and `stop_unknown` or `start_unknown`. If a call times out, the
watcher remains `NEEDS_ATTENTION` and never retries that effect. The operator
must preserve this journal, inspect the exact Docker container/lifetime and
actual namespace and listener, and explicitly reconcile the unknown outcome
before planning a new effect. Stop the watcher, then, with the same protected
config and trusted host owner, run the watcher command with `--reconcile`.
It acquires the same exclusive lock and never issues Docker effects. It checks
the exact recorded manager lifetime, proxy identity and protected config
twice; it accepts a stopped proxy for `stop_unknown` or a running proxy in the
manager's namespace for `start_unknown`. It saves the original unknown record
and observations in an exclusive owner-only `proxy-resolution-*.json`, fsyncs
them, then advances the controller state. An unavailable, changed or ambiguous
observation remains `NEEDS_ATTENTION`; do not edit the journal or retry the
Docker effect. Restart the watcher only after an accepted observation. These
observations do not prove who caused the effect or authenticated readiness.
A durable `stopped` record can resume its single
start only while the pinned manager lifetime is unchanged and the pinned proxy
is independently observed stopped. `done` proves only matching namespaces;
it is not an HTTP, OAuth, management-child or worker readiness assertion.
