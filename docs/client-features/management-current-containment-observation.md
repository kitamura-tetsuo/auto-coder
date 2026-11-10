# Read-only current containment prerequisite observations

`python -m auto_coder.management.containment_observation` provides a bounded
**unavailable prerequisite report**, not a successful closed-profile observation.
Issue #2827 is not fully implemented by this adapter. Neither selector currently
has an accepted production profile. The existing `fresh_host` capture/claim/guard,
replacement admission, retained journal and original owner boundaries are unchanged.
No recovery launch or admission from #2824/#2825 is implemented here.

The explicitly versioned selectors are `ordinary-v1` and
`quarantined-original-v1`. They identify requested review targets, not reviewed
original source evidence. The adapter must not infer that today's installed
bootstrap bytes were the original deployed bytes. The ordinary source currently
in this checkout takes `.supervisor-owner.lock` exclusively in
`ManagementBootstrapSupervisor.run` before calling `_run_owned`; that source inspection
does not bind an original deployment to that implementation or owner inode.
The quarantine selector additionally reports the absent exact bootstrap file
mounts, `no-new-privileges` setting, and support-mount purpose, source, destination,
read-only and execution binding. It does not accept a quarantine configuration as
a replacement profile or loosen the existing replacement checks.

## Interface and available facts

Create a scope selection JSON with exactly these keys:

```json
{
  "deployment_id": "proposed-deployment",
  "enrollment_id": "proposed-enrollment",
  "authorization_id": "proposed-authorization",
  "profile": "ordinary-v1",
  "management_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "proxy_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "files": []
}
```

IDs here select a proposed scope only. `files` can select up to 32 sorted unique
absolute file paths, for example the installed controller source and effective
configuration. Canonical regular files of at most 1 MiB are read without following
symlinks. The report includes their device, inode, size, change/modification times
and SHA-256, never file contents. It checks their identities again after Docker
observation. File hashes are observations, not independent enrollment approval or
proof that the configuration is immutable. Files, stores and owner locks are not
created, locked, repaired or modified by these reads.

```sh
python -m auto_coder.management.containment_observation observe \
  --scope /private/proposed-scope.json > /private/prerequisites.json
python -m auto_coder.management.containment_observation verify \
  --scope /private/proposed-scope.json --previous /private/prerequisites.json
```

Both commands return exit status **2**, even if every available sub-observation
matches. Output is JSON. `current-containment-prerequisites-v1` includes the full
proposed scope and its hash, a new observation UUID, current boot ID, observer PID
and process-start token, monotonic observation interval, observed file identities,
available Docker identities and explicit unavailable authorities/reasons.
Malformed inputs also return 2 with an unavailable diagnostic. Duplicate JSON
keys, arbitrary profile names, relative paths and ambiguous container IDs refuse.

The Docker reader only uses `context inspect`, `info` and container `inspect`.
It requires the local `/var/run/docker.sock` context and refuses Docker endpoint
overrides. Complete inspection hashes bind selected container IDs to the engine,
image, effective command, mounts, security options, restart configuration and
current Docker lifetime without exposing environment/configuration contents.
Two independent reads must agree. This selected-pair inspection does **not**
enumerate all deployment containers, close launch routes or prove task absence.

The verifier independently reads the selected current files and Docker facts;
it never accepts a saved success assertion. It compares scope, boot, file and
Docker observations and resource limits, and reports discrepancies. Its fresh
process and observation identities differ from the producer. Agreement does not
remove any missing prerequisite. A saved object remains unavailable after the
producer exits, and is never a launch grant, historical completeness claim,
continuous exclusion, or reservation of a listener/namespace.

## Missing authoritative prerequisites

An accepted production observer still requires initial-namespace Linux host root,
unrestricted host procfs/cgroup visibility and local Docker authority. More
importantly, it requires original authoritative deployment provenance or a
separately approved present-time enrollment and an implemented reader for that
authority. Merely naming an enrollment or supplying operator lists is insufficient.
No enrollment or controller mutation is performed here.

The installed route inventory must independently cover the actual watcher and
explicit reconciliation, services/timers, socket activation, Docker restart
policies, configured local and remote scripts, repair/recreate routes, and
alternative roots. This adapter cannot establish that inventory and always
reports it missing. Unknown, omitted, enabled, uninspectable or mutable alternative
routes cannot be declared outside the deployment. It does not impose a new threat
model on trusted Docker, Git, worktrees or later administrator actions.

An accepted observer also needs the exact original manager/proxy/task and boot
lifetimes, all cgroup tasks including nonleaders, currently pinned namespace
objects, current occupancy and endpoint listeners, and exact original owner-file
fencing evidence. No such pins or fences are held by this report. Saved inode
strings, stopped Docker state and inaccessible tasks cannot prove absence or
exclude escaped tasks, namespace reuse, zombies with uncertain lifetimes, or
observation races. Existing conservative host observation helpers are unchanged.
All temporary read descriptors are closed before return, including refusals.

## Validation boundary and remaining Issue coverage

Run `bash scripts/test.sh --ci-tests-only
tests/test_management_containment_observation.py`. These regressions invoke the
actual checked-out module in separate real Linux processes, fingerprint actual
bootstrap source, detect changed controller source and alternate selected roots,
reject cross-deployment/enrollment/authorization/profile replay, preserve selected
retained-file contents and identities, and independently reobserve after producer
exit. A deterministic unit interleaving supplements these tests for file mutation;
it is not production authority or an acceptance substitute.

These tests establish part of REQ-401/404/405/406's refusal and interface behavior.
They do not establish complete REQ-401/402/403/404/406 compliance. All AS-T01–AS-T05
production-host acceptance scenarios remain unpassed: positive exact-source
ordinary/quarantine containment, real watcher/service/timer/socket/Docker route
inhibition and omission detection, unselected alternate roots, escaped/nonleader
tasks, namespace reuse/occupancy and conflicting listeners, exact mount/security
substitution, controller races, and original-store/worker/OAuth preservation across
real host attempts require the missing deployment authority, exact original
profiles and isolated root/Docker/controller harness. The implementation sandbox
denies Docker socket access, TCP socket creation and Unix socket binding, and
runs as UID 1000, so those fixtures cannot run here. The bounded validation run
also exercises the unchanged `tests/test_management_fresh_initialization.py`:
its four socket-dependent cases fail with `PermissionError: Operation not
permitted` in this sandbox. They are not skipped, weakened or counted as passing.
No successful synthetic-profile test is reported as acceptance.

This path creates no operation admission/dispatch, worker effect, OAuth change,
recovery-success event, or Issue/PR dashboard trace. Its unavailable JSON is local
diagnostic output only. Later integration of accepted evidence into #2824 owns
the corresponding production trace-to-view obligations.
