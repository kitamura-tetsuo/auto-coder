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

For a disposable environment, create an owner-only `0700` state directory and
an owner-only `0600` JSON configuration containing `state_root`, exact full
`manager_id` and `proxy_id`, and their full `manager_image` and `proxy_image`
digests. Start `python -m auto_coder.management.proxy_watch_cli --config
/absolute/path/to/config.json` as the trusted host owner after Docker is
available. One locked process polls; it does not start the manager or report
authenticated readiness. A new manager container ID requires a separately
reviewed proxy definition and config, not substitution of a name.

Before each Docker effect the watcher durably records the exact manager
lifetime and `stop_unknown` or `start_unknown`. If a call times out, the
watcher remains `NEEDS_ATTENTION` and never retries that effect. The operator
must preserve this journal, inspect the exact Docker container/lifetime and
actual namespace and listener, and explicitly reconcile the unknown outcome
before planning a new effect. A durable `stopped` record can resume its single
start only while the pinned manager lifetime is unchanged and the pinned proxy
is independently observed stopped. `done` proves only matching namespaces;
it is not an HTTP, OAuth, management-child or worker readiness assertion.
