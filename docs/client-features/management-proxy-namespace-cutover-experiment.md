# Management proxy namespace experiment

Status: isolated experiment only; not a production cutover procedure.

The installed proxy uses Docker `network_mode: container:<manager-id>`. A
running proxy can retain the old network namespace after a manager restart;
container status and Docker restart policy do not demonstrate connectivity.

`sudo python3 tests/manual_proxy_namespace_validation.py` creates disposable
manager and Caddy containers with unique names and a nonsecret HTTP marker. It
verifies the initial namespace and HTTP response, restarts the manager while
the proxy remains alive, and tests whether their actual `/proc` network
namespace identities diverge. When they do, it checks failed HTTP access,
invokes a one-shot automated reattachment of the retained proxy and checks
both namespace identity and HTTP response. It then injects failure after a
confirmed proxy stop and checks that no automatic retry occurs. It starts a
successor and restarts the retained proxy against the same manager, performs a cutover to a distinct
manager and successor proxy, and rolls back to the retained pair. It removes
only its randomly named disposable containers.

On the tested host the namespace diverged after manager restart; all HTTP and
namespace checks for replacement and rollback passed. The automated action is
invoked by this test after divergence is detected; no persistent Docker event
watcher or production startup integration is implemented. This test does not use
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
