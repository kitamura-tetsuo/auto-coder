# Operator-approved management bootstrap upgrade

`python -I -B -m auto_coder.management.bootstrap_upgrade` is a trusted **host**
operator command. It joins the existing fresh-initialization, installed READY,
ancestor-exclusion and source-evidence readers for a management bootstrap and
its attached proxy. It is not an MCP tool, source selector, worker deployer,
Docker/Compose manager, credential provisioner or automatic rollback.

## Supported host profile

Run on the Docker host as the owner of the protected files (normally root),
with a complete initial-namespace `/proc`, Docker CLI, Python installation of
the matching auto-coder source, and the existing host fresh/exclusion profile.
The old management and its proxy must still be live for preparation. The
replacement management and proxy must be distinct **created but stopped**
Docker containers with pinned `sha256:` image IDs and fully reviewed bind
mounts; prepare does not create or replace containers. The replacement must
use the real one-shot `--fresh-initialization` bootstrap command, bridge plus
loopback published endpoint, read-only root, source-read extension, authenticated
installed first runtime and complete inherited ancestor bindings. Prepare the
separate successor state/journal/guard and watcher directories as mode 0700.
Do not mount the Docker socket into management or grant it host privileges.

Create `/var/lib/auto-coder/management-upgrades` with owner-only mode 0700
before preparation. The following JSON describes **host-side absolute paths**;
each path must be canonical and pre-provisioned, without symlink components.
Save the file outside that directory as an operator-owned 0600 regular file.
It is deliberately a closed schema, not a script or list of arbitrary effects.

```json
{
  "evidence_file": "/private/next/guard/prior-evidence.json",
  "intent_file": "/private/next/fresh-intent.json",
  "bootstrap_config": "/private/next/bootstrap/config.json",
  "management_config": "/private/next/bootstrap/management.yaml",
  "prior_lock": "/private/prior/state/.supervisor-owner.lock",
  "guard_dir": "/private/next/guard",
  "mounts_file": "/private/approved-prior-mounts.json",
  "successor_mounts_file": "/private/approved-successor-mounts.json",
  "successor_proxy_id": "<exact 64-character Docker container ID>",
  "successor_proxy_image": "sha256:<exact 64-character image ID>",
  "successor_proxy_config": "/private/next/Caddyfile",
  "old_proxy_config": "/private/prior/Caddyfile",
  "old_watcher_config": "/private/prior/proxy-watch.json",
  "new_watcher_config": "/private/next/proxy-watch.json",
  "source_sha": "<exact 40-character tested source commit>"
}
```

Both mount JSON files contain exactly `management` and `proxy` lists of
`[host_source, container_destination, read_write_boolean]` triples. These
triples must describe the actual inspected Docker objects, not desired
mounts. Every referenced configuration, intent, image and source-evidence
extension must be prepared under the existing feature-specific producer's
rules; no configuration is silently rewritten here. See
`management-fresh-initialization.md`,
`management-archived-exclusions.md`,
`management-source-evidence-configuration.md` and
`management-runtime-staging.md` for the underlying contracts.

The old proxy watcher must have been cleanly disabled by its **existing host
service owner** before `apply`. The coordinator acquires its exclusive lock and
holds it across the cutover; it will refuse an active or respawning watcher,
not stop an unknown process. The new watcher must have a **separate** protected
state root. Review the host service manager's restart policy before accepting
the upgrade; neither preparation nor this command changes service units.

## Operator approval and commands

The protected source credential is the already provisioned file
`source_evidence_read` in the management YAML directory, exposed read-only in
the candidate at `/bootstrap/source_evidence_read`. The matching protected
`management.yaml.source-evidence.json` must name the exact repository
`kitamura-tetsuo/auto-coder` and bind the original YAML SHA. The operator must
separately accept **each** credential-read invocation. The command never
creates, exports, prints, stores, broadens, or guesses a credential or its
GitHub scopes. Only an actual successful read of the tagged exact tested source,
source shard and required Fresh host integration Actions run supplies evidence;
filesystem mode alone is not GitHub authority.

```sh
python -I -B -m auto_coder.management.bootstrap_upgrade prepare \
  --profile /private/approved-upgrade-profile.json \
  --auth-token-file /private/operator-authenticated-client-token \
  --authorize-source-read
```

`prepare` validates actual Docker observations, worker lifetimes, old owner
locks/ancestry, retained OAuth and operation stores, installed candidate READY,
source provenance, mounts, and protected configuration bytes. It has no live
container stop/start effects. It prints a content-addressed `plan_sha256`.
Review the protected plan at
`/var/lib/auto-coder/management-upgrades/<plan_sha256>/upgrade-plan.json`,
including exact predecessor/successor IDs, old process generations, source
proof attempts, worker baseline, OAuth/grant digests, and every effect input.
Reviewing only the returned hash is insufficient authorization.

```sh
python -I -B -m auto_coder.management.bootstrap_upgrade apply \
  --plan-sha256 YOUR_REVIEWED_FULL_SHA256 \
  --auth-token-file /private/operator-authenticated-client-token \
  --authorize-source-read
python -I -B -m auto_coder.management.bootstrap_upgrade status \
  --plan-sha256 YOUR_REVIEWED_FULL_SHA256 \
  --auth-token-file /private/operator-authenticated-client-token \
  --authorize-source-read
```

The exact plan is accepted once. Under the held host/ancestor fence it sends
only graceful `SIGTERM` to the pinned old proxy/management, then `docker start`
for the already created new management/proxy and launches its exact new
watcher. There is no forced kill or Docker object removal/recreation. The
successor consumes the original one-time host fresh guard/claim. The controller
never starts workers or performs beta deployment.

An intent is fsynced **before** each physical effect; start generations and
the watcher lifetime are retained as receipts when observed. A lost response,
interrupted host session, uncertain launch or broken continuous fence leaves
the original operation intact. A second `apply` must refuse. `status` is
read-only with respect to Docker and source selection; it reports which effect
was last sent, the original target, independent physical observation, and
`needs-attention` where a safe continuation cannot be proven. Do not delete
or recreate the lane, state/journal, launch claim, container or predecessor
image to get past a refusal. The supported next action in uncertain cases is
operator inspection of the original host evidence; recovery that uses a new
instance/plan requires separate explicit review. A prior-generation restart
could bypass successor admissions and is **not** an automatic rollback.
The command prints an operator result to standard output and records the same
non-secret report and Loguru diagnostics in the mode-0600
`/var/lib/auto-coder/management-upgrades/upgrade-audit.log`. Authenticated
MCP readbacks use a fixed direct loopback connection and never follow a
redirect with the bearer credential.

`status` distinguishes completed effect dispatch from live application
readiness. The latter requires the original fresh `succeeded` outcome
(including protected unauthenticated challenge and exact installed-child
readback), current authenticated management whoami and configured worker reads,
stable management/proxy launch generations, retained OAuth/grant identity and
the active new watcher attached to the successor namespace. An unavailable
authenticated client token leaves a specific prerequisite; it is never a
host-control bearer.

With separate `--authorize-source-read`, `status` asks the **upgraded
management service** for authenticated `auto_coder_beta_source_readback` for
each configured beta target. This read-only tool uses the same protected
credential, current-selection reader, original tested-source tag, selected
workflow run, and required job verification as the production beta deployer.
The beta-selected source revision is independent of the candidate management
image revision; their original Actions attempts must not be conflated.
The operator does not call a separate host-side fixture/proof producer in place
of the upgraded service. Unavailable actual child evidence, missing credentials,
a moved selection, or disagreement across beta targets leaves only
`application-ready` and a precise missing prerequisite. Even a previously
recorded `beta-deploy-prerequisites-verified` result is not a durable
authorization: every subsequent `status` first returns its persisted stage to
`application-ready` and only promotes it after a fresh authorized successful
read from the actual upgraded child. A missing read approval, disappeared
selection or conflicting pointer must never leave stale persisted beta-ready
authority. The tool does not select a ref, enqueue an operation, start or stop
workers, or deploy beta.
`beta-deploy-prerequisites-verified` is **not** beta deployment or proof of
completion. The actual beta source deployment remains a separate authorized
MCP operation using the existing policy, worker bridge/generation and exact
source admission.

An A→B→C sequence retains each previous plan under its original digest
directory. A new preparation is rejected while any earlier operation is
unfinished or uncertain. Original OAuth, predecessor/current and archived
journals and intent records must remain unchanged and separately inspectable;
no history merging, archive projection, idempotency-key retry or direct-prior
online replay is performed by this host coordinator.

## Verification

Unit checks are in `tests/test_management_bootstrap_upgrade.py`. Fresh host,
archived exclusion, proxy watcher, installed-runtime and source deployment
production-origin tests run in the **Fresh host verification** and other
required supervisor integration CI jobs. Local non-root/procfs-restricted
environments cannot establish native `/proc/locks` or Docker host proof, and a
skipped local integration test does not authorize a live cutover. Check the
exact candidate SHA's required CI jobs/cases before review or operator use.
