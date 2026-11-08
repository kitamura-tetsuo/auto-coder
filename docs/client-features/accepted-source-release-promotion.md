# Explicit accepted Git-source release promotion

The operator-only `Promote Accepted Source Release` workflow publishes one
**explicit** tested Git commit as a Git-source release. It never samples the
latest beta, starts the release worker, builds an OCI image, changes a GHCR tag,
or grants Docker rights to the runtime. The selected release source is a Git
commit, not an image digest. Actual release-worker installation and health
verification remain separate, explicitly authorized operations.

## Operator contract

Dispatch `.github/workflows/promote-accepted-source-release.yml` from `main`
with a stable bounded `operation_id` and all of the following inputs:

| Input | Identity that must be matched independently |
| --- | --- |
| `source_repository`, `source_sha` | Exact operator-configured repository and full candidate Git commit |
| `ci_run_id`, `ci_run_attempt` | Original completed-success source-test Actions attempt, not the latest retry |
| `tested_source_ref`, `tested_source_oid`, `tested_source_record_sha256` | Exact published immutable tested-source ref, annotated object, canonical record digest |
| `acceptance_ref`, `acceptance_oid` | Actual published `source-acceptance-v1` annotated ref/object, with direct commit target |
| `release_target` | Fixed operator-configured `release` source channel |

The CLI command is `auto-coder deployment promote-accepted-release` with the
same required arguments plus `--promotion-run-id` and
`--promotion-run-attempt`. Workflow run/attempt identity is obtained only from
the Actions environment in the fixed workflow; its exact workflow path, event,
repository, and `main` branch are independently read through Actions REST.
All input values travel through environment variables into quoted shell
arguments. No input value interpolates into the `run:` script, chooses an
Actions workflow or Git remote, or supplies an operator policy.

The checked-in operator-owned policy
`.github/accepted-source-release-policy.json` is read from the trusted
`main` checkout. It fixes the source repository, release target and Git ref,
allowed beta instance IDs, exact permitted beta-runtime-smoke profile IDs and
content revisions, original Actions identity, and finite evidence lifetime.
Unknown, absent, duplicate, outdated, or unsupported policy data blocks
publication. The default policy permits the `beta-worker` instance and the
default `beta-runtime-smoke-v1` revision and limits evidence age to 24 hours.
Operators can deliberately revise that tracked policy through normal code
review; an acceptance annotation or workflow/MCP caller cannot revise it.

## Qualification and immutable evidence

The validator independently fetches both Git annotated tags and their direct
commit targets. A `local_result=PASS` claim alone does not mean published
acceptance. The exact `source-acceptance-v1` tag must contain the canonical
`beta-source-acceptance` schema-v1 annotation from #2605 and its original
`beta-runtime-smoke-v1` content/digest. The candidate, source-test proof and
original Actions attempt must all be equal. The recorded beta selection and
the two separate original CI attempts (proof and selected execution) are
validated against their immutable history and Actions jobs; current beta
selection is *not* consulted.

The original installed beta capture must bind the same worker/Supervisor
incarnation, locked source and distribution, READY content identity and runtime
ID to the successful deployment. Every recorded qualifying sample must show
the original required startup/readiness checks, running worker and invocation
gate, producer-origin heartbeat, exact runtime provenance and bounded sample
time. At least two unique authentic worker-produced observations must have
strictly advancing heartbeat sequences inside the original finite deadline;
manager-generated progress, elapsed time, nonce responses and diagnostic reads
cannot qualify. An internally inconsistent PASS annotation is refused even
when its local content digest and Git tag are internally self-consistent.
After acceptance has finished, beta selection and the installed beta worker
may change; neither is resampled or required to remain selected. Eligibility
still requires an allowed original profile, unexpired evidence, successful
original CI, and intact immutable publication readback.

## Catalog, pointer and restart semantics

One operation creates the versioned `git-source-release` schema **2**
annotated catalog/tag `refs/tags/auto-coder-source-release-<operation_id>`.
This tag directly targets the tested commit and immutably binds the exact
acceptance/local content IDs, CI run and attempt, tested-source ref/object,
promotion Actions run identity, operator policy revision, configured release
Git ref and its observed prestate. It has no OCI `image` or `digest` fields.
Creating the catalog ref is create-only; any existing conflicting annotation,
object, or commit is a collision. Catalog readback must succeed **before** any
mutable release pointer update.

The only mutable selection is the configured direct commit ref
`refs/tags/auto-coder-release-source-current`. Its prestate is independently
observed and checked again immediately before the one permitted update. The
workflow shares `release-channel` concurrency with the legacy OCI promotion
and restore workflows, with `cancel-in-progress: false`. GitHub's Git ref API
has no global transactional compare-and-swap over external writers: a trusted
administrator outside this Actions lane can still race the operation. Changes
or unreadable postconditions are reported as `SUPERSEDED` or
`NEEDS_ATTENTION`, not as a guaranteed atomic transaction. Operators must
serialize any other Git-source release or restore writer through the same
lane. Old OCI workflows remain separate, explicit operator actions and do not
write this source ref.

Only after the exact catalog and current ref read back does the operation
create or verify a GitHub Release associated with that immutable operation
tag. Completion requires independent re-reading of the annotated tag/object,
direct commit pointer, and exact GitHub Release. A successful write response
alone does not establish completion or release-worker health.

On repeat workflow attempts the original catalog is read **first**. A
different candidate, proof, acceptance, policy revision, or Actions run is
rejected. When its intended source pointer is already independently confirmed,
an absent GitHub Release can be published/read back without moving selection
again. An ambiguous missing record, absent ref, changed pointer or lost
catalog/pointer readback is held for operator attention; retry never selects
another candidate or reissues a possibly completed pointer write. A retry
without the original catalog cannot start a new release mutation.

## Historical OCI compatibility and verification

Historical version-1 image catalogs remain readable through
`auto-coder deployment release-catalog read` and the existing release/restore
commands. `promote-release` and `restore-release` still require explicit
separate operator intent and can only move legacy OCI image tags; they do not
select, restore or overwrite the new Git source pointer. Source-schema-v2
records are never passed to those image consumers or assigned an invented
digest. The previous release/beta channels and their image documentation
remain valid as historical/OCI operator tools.

Regression coverage exercises exact original producer annotations, alternate
source commits, tampered content and heartbeat/predicate claims, moved beta
selection, stale policy/evidence, conflicting operations, lost responses and
race/readback uncertainty at Git ref and Actions boundaries. Controlled
transport tests do not claim an actual release-worker deployment; a live
end-to-end release and external-admin race remain independently observable
operational checks.
