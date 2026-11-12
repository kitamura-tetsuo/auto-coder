# Original ordinary deployment genesis and retained lineage

The explicitly selected `ordinary-original-genesis-v1` producer supplies the
prospective original baseline required by the recovery epoch's lineage reader.
It implements the original-evidence responsibility in #2839. It creates no
recovery epoch, issues no recovery grant, and does not authorize a deployment.
The existing ordinary bootstrap is still the separately authorized process
that launches its configured management child.

An original deployment cannot be inferred from an empty old directory or a
currently stopped Docker object. This profile therefore has two distinct
original capture boundaries before the ordinary bootstrap can run:

1. `prepare_original_genesis_roots` consumes a protected explicit new-deployment
   authorization. It first creates an exclusive original claim, then creates
   the state, management-operation journal, OAuth and history directories with
   `mkdir` and no `exist_ok`. Every preexisting path is refused. The original
   root receipt binds the actual directory inodes, the selected protected
   configuration files, producer source and Linux process lifetime. A partial
   claim remains unavailable and is never repaired or retried.
2. `capture_original_genesis` consumes the separate final authorization naming
   the exact manager and paired proxy Docker IDs. It requires both objects to
   be `created`, with zero PID, zero restarts and never-started timestamps. It
   compares the complete local-engine inventory, effective original commands,
   protected configuration mounts, original root identities, and the complete
   installed Python package with the executing reviewed producer source. The
   selected manager and proxy may not have other discovered management routes.
   This capture reads Docker only; it does not create, start, stop or reconfigure
   Docker objects.

These observations do not convert a legacy baseline into original evidence.
The root creation receipt must already exist, and its authorization and
configuration identities must match the final selection. Existing or partial
history, a reused authorization claim, replaced roots, changed source,
conflicting original files or a previously started object remain unavailable.

## Explicit source and configuration selection

`BootstrapConfig.original_genesis` is optional. Selecting this producer requires
its exact value `/bootstrap/original-genesis.json`, the ordinary bootstrap
command and the fixed installed management console entrypoint. It cannot be
combined with fresh initialization, NEW initialization, inherited exclusions
or the installed fresh-runtime selector. Omitting it preserves existing
ordinary behavior. NEW/#2672 and ordinary fresh/upgrade refusal behavior are
unchanged.

The selected ordinary manager has these original mounts:

| Role | Container path | Access |
| --- | --- | --- |
| Original bootstrap configuration and genesis files | `/bootstrap` | Read-only |
| Original child, handoff and recovery stores | `/management-child-state` | Read/write |
| Original management-operation journal | `/management-operations` | Read/write |
| Originally created OAuth root | `/management-oauth` | Read/write |
| Original history authority and lock | `/management-original-history` | Read-only |

The same mapped inodes are independently observed by the host and bootstrap.
The fixed configuration members are `/bootstrap/config.json` and
`/bootstrap/management.yaml`. The paired proxy has the ordinary Caddy command,
its exact protected configuration bind mount, and the manager's network
namespace. All original files are owner-only regular files with one link;
protected original directories have mode `0700`.

Root authorization JSON contains exactly the fields of
`GenesisRootAuthorization`: `profile` (`ordinary-original-roots-v1`), `purpose`
(`separately-authorized-new-deployment`), `deployment_id`, canonical UUID
`authorization_id`, `bootstrap_path`, `management_path`, `proxy_config_path`,
`state_root`, `journal_root`, `oauth_root`, and `history_root`. The four root
paths must not yet exist, and their parent directories must already be
protected. Configurations and authorization files must already be protected.
The writer records `original-genesis-root-claim.json` and
`original-genesis-roots.json` beside the bootstrap configuration.

The later `GenesisSelection` has `profile` (`ordinary-original-genesis-v1`),
`purpose`, `deployment_id`, `authorization_id`, `management_id`, `proxy_id`,
`history_root`, `bootstrap_path`, `management_path`, `proxy_config_path`, and
`root_authority_path`. Its deployment, authorization, configuration and history
scope must match the actual original root authority. Docker IDs are exact
64-character IDs, not names or mutable tags. Both inputs use canonical JSON.

The host-root CLI stages are:

```sh
python -m auto_coder.management.original_genesis \
  --prepare-roots /protected/new-deployment-root-authorization.json \
  --log-file /protected/logs/original-root-capture.log

# The separately authorized deployment workflow creates the exact ordinary
# manager and proxy with the original roots, then writes the final selection.
python -m auto_coder.management.original_genesis \
  --authorization /protected/new-deployment-final-authorization.json \
  --log-file /protected/logs/original-genesis-capture.log
```

This module does not perform the deployment workflow represented by the comment.
Loguru writes producer diagnostics to the selected file and stdout. Log files
must be outside the pristine root namespaces during host capture.

## Native bootstrap boundaries

Before any child or management effect, the actual `_run_owned` ordinary path
opens its original owner file independently and obtains its exclusive kernel
flock. Its first owner uses exclusive file creation; an interrupted first
owner cannot be reclassified as an untouched root. The bootstrap obtains a
shared history flock through the original read-only mount, verifies the exact
original authority and genesis, checks current source/configuration/root
identities, and requires the original history to remain empty for its first
native initialization.

The existing native initializers create all four original SQLite stores under
an owner-only umask. The bootstrap durably records their exact inodes in
`original-genesis-stores.json`, followed by
`original-genesis-first-launch.json`, which binds the exact first operation,
attempt, producer lifetime and native receipt. The normal ordinary launch path
uses those same operation and attempt IDs. These records describe original
boundaries; neither record asserts that a child actually launched or that the
application became ready.

An interruption before a required native record remains unavailable. The
bootstrap cannot recreate missing originals or replay the first owner. An
originally recorded but unresolved first launch retains its unknown outcome.
Normal store and unknown-state refusal rules continue to apply on later
bootstrap reconstruction.

## Full retained lineage API

`original_attempts.read_frontier` recognizes `ordinary-original-history-v1`
authority in addition to the existing prospective fresh-attempt authority.
The new authority pins the exact original genesis file, which fixes the
baseline instance and Docker object before the first successor exists.
Every supported later ordinary fresh, upgrade and exclusion-cutover host
preflight must preserve that same history binding. A successor cannot select
an arbitrary first predecessor or reuse a baseline protected root. All
original failed branches remain in the append-only frontier.

`original_attempt_evidence.derive_lineage(binding, history_fd, owner_fences)`
and `verify_lineage(submitted, binding, history_fd, owner_fences)` independently
read original genesis, every retained frontier event and every expected native
store. The caller holds its own current history shared flock and exactly one
current shared owner flock for each native generation. A never-created branch
has its affirmative original terminal boundary and no invented owner file.
The reader joins each native successor to its predecessor's exact owner inode
and the original retained OAuth root.

The frozen `OriginalLineageEvidence` exposes original `genesis`, `attempts`,
`generations`, `originals`, `frontier`, and `unavailable` reasons. Each
`OriginalGeneration` includes the exact management/source/configuration/root
scope, native owner identity, expected stores and factual `outcome`. The
original baseline also pins its proxy identity. A fresh successor's proxy is
not invented from an intent that never recorded one; current containment must
independently establish that pair.

Complete original evidence with an `unknown` or unresolved application outcome
is distinct from missing original evidence. For example, the native store
receipt is produced after the original first-launch claim, so a missing claim
is a provenance gap even if a status reader can report `unknown`. The lineage
reader preserves the unknown and every retained native record. It never
checkpoints SQLite, clears unknowns, repairs evidence, rewrites exclusions, or
issues launch authority. A nonempty unresolved WAL or rollback journal remains
unavailable. Existing `derive`/`verify` prospective-reader behavior is preserved.

## Verification boundaries

`tests/test_management_original_genesis.py` exercises actual protected
filesystem publication, affirmative root creation, original claim refusal,
root/source/configuration pinning, actual native store initialization under an
exclusive owner flock, shared-fence independent derivation, preserved unknown
outcomes, complete failed branches, and missing-original refusal. Docker
observations and container bind mapping are explicit component substitutes;
these tests are not real-Docker, first-child or recovery-epoch acceptance.

The production interfaces are suitable for isolated real-Docker and host-root
acceptance. Full #2824 acceptance additionally requires the actual complete
current containment verifier, issuer, OS descriptor transfer, consumer receipt
and durable epoch boundaries. Component provenance tests do not substitute for
that integrated acceptance.
