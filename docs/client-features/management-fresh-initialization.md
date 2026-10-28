# Explicit fresh management initialization

Fresh initialization creates a distinct management instance and new child,
operation, handoff, and recovery stores while retaining an existing OAuth root.
It is an operator-authorized first launch, not recovery of prior management
history. Missing stores in an existing instance continue to fail closed.

The host operator prepares separate owner-only state, operation and guard
directories and an immutable, read-only replacement image. The selected
bootstrap and management configurations must describe the new instance,
existing OAuth root, separate new roots, exact selected runtime and fixed
endpoint. The replacement must mount the prior owner lock read-only at
`/management-prior-owner.lock` and the private guard directory read-only at
`/management-initialization`; its bootstrap config fixes the guard socket and
prior lock paths. `PYTHONDONTWRITEBYTECODE=1` and a read-only container root
keep the recorded application closure stable. This path does not grant Docker
access or additional capabilities to the management child.

Before the first launch the new supervisor opens the retained OAuth database in
immutable read-only mode. It requires the expected complete schema, matching
issuer/resource/project and token scope columns already populated. A missing
database, nonempty WAL, schema migration, corrupt data or changing database is
unavailable; it is not replaced, migrated or silently interpreted as empty.

On the host, `python -m auto_coder.management.fresh_host capture` records the
exact live management and proxy containers, image identities, supervisor lock,
Linux process lifetimes, namespaces and cgroups. The operator supplies the exact
approved bind-mount profile in a private JSON file. Only supported, unprivileged
container profiles with the expected mounts are accepted. The evidence file is
created once in the new owner-only guard directory. Access errors are refusal,
including inability to inspect an old process namespace.

The host commands accept only explicit, private files and exact container IDs:

```sh
python -m auto_coder.management.fresh_host capture \
  --management-id OLD_MANAGEMENT_ID --management-image OLD_IMAGE_DIGEST \
  --proxy-id OLD_PROXY_ID --proxy-image OLD_PROXY_IMAGE_DIGEST \
  --mounts-file /private/approved-mounts.json --guard-dir /private/guard
python -m auto_coder.management.fresh_host claim \
  --intent-file /private/fresh-intent.json --evidence-file /private/guard/prior-evidence.json \
  --prior-lock /private/old-state/.supervisor-owner.lock --prior-network-fd "$OLD_NETWORK_FD"
python -m auto_coder.management.fresh_host guard \
  --state-root /private/new-state --evidence-file /private/guard/prior-evidence.json \
  --prior-lock /private/old-state/.supervisor-owner.lock --guard-dir /private/guard \
  --prior-network-fd "$OLD_NETWORK_FD"
```

The mount file has `management` and `proxy` arrays of `[source, destination,
read_write]` entries and is mode 0600. The intent is a mode-0600 JSON object
with the exact fields of `FreshInitializationIntent`; it includes separate host
and container roots, their device/inode identities, and the digest printed by
`capture`. These are local operator inputs, never OAuth credentials. Capture
occurs while the prior containers are still running; claim and guard occur only
after the approved stop and while the old owner fence is continuously held.

After the operator stops the old proxy and management, a continuously held shared
lock on the old owner file prevents its supervisor from acquiring its exclusive
lock. The operator creates, but does not yet start, the exact replacement
container; prepares an intent bound to its ID, immutable image, configuration,
application and executable digests, prior evidence, lock and mounted-root
identities; and uses `python -m auto_coder.management.fresh_host claim` to write
it once to the new state root. The host `guard` command retains the old lock,
checks old container and cgroup absence, the old network namespace, and the host
listener, then waits for the replacement. The guard is a foreground one-shot
host process; it does not stop, start, or control Docker containers.
The operator must open the old supervisor's `/proc/PID/ns/net` while that exact
captured lifetime is still running, keep the descriptor open throughout claim
and guard, and pass it to both commands as `OLD_NETWORK_FD`. The retained
namespace object prevents its inode from being reused by the replacement. A
missing, closed or mismatched descriptor refuses the transition.
For example, in the same long-lived Bash process that runs those commands,
`exec {OLD_NETWORK_FD}<"/proc/$OLD_PID/ns/net"` opens the pin. The exact
`OLD_PID` must first be matched to the captured Docker container lifetime.

For a supported staged Docker cutover, keep the old management and proxy
container objects stopped and inspectable through admission. Do not use Compose
recreate on the old project, since it removes the objects used as evidence.
Prepare a separately named replacement project and its exact container ID, hold
the old lock continuously across claim, guard, and replacement admission, and
only then start the replacement. The host checks the replacement's loopback
port mapping and, at grant time, requires the single listening socket to be
owned by Docker's userland proxy for that exact container address. Other
listeners, inaccessible proxy descriptors, and NAT-only publishing are refused.
The old container objects and their configuration and state remain available for
operator assessment; a rollback must respect any fresh launch claim.

Start the replacement supervisor with `--fresh-initialization`. It checks the
intent and input bytes, takes its own shared hold on the old owner fence,
publishes its lifetime, and requests a fresh challenge from the host guard. The
host joins the peer to the exact running replacement container, its image,
read-only root, approved state mounts, supervisor ledger and kernel lock, then
rechecks prior absence before replying. The supervisor records a durable
one-attempt launch claim before creating the new stores and launching. An
interrupted or uncertain claim, missing host evidence, replaced lock or mount,
listener conflict, changed bytes or partial state refuses rather than retrying.

`auto-coder management fresh-status --config PATH` reads the local status. A
`launched` status includes a non-secret `observation_id`; a currently authorized
OAuth client calls `auto_coder_whoami` with that ID. A separate protected HTTP
challenge and the fresh exact-child readback are required for `succeeded`.
Timeout, process exit, unavailable evidence or an interrupted transition never
means success and never authorizes another launch. OAuth credentials and grants
are neither copied nor used as host process-control authority.
Physical self-update effects remain held until the fresh launch has authenticated
readiness; recovery requests are refused during that interval. An unknown
outcome does not release either fence.

The host operator must preserve the old image, configuration and stores and
review a deployment-specific stop, verification and rollback plan before any
live change. Rollback must not erase the fresh intent or an unresolved launch.
