# Durable local beta runtime-smoke evidence

`auto_coder.management.beta_runtime_smoke.BetaRuntimeSmokeProducer` produces
one durable, local `beta-runtime-smoke-v1` result for an already authenticated
acceptance operation. It reads the exact selected Git source, the successful
worker deployment, and the actual installed beta worker through the existing
Supervisor network bridge and worker Unix socket. A local result remains
readable when Git publication is unavailable.

The producer does not register an MCP tool, authenticate a caller, issue
credentials, admit another operation, acquire another lane, or run a second
dispatcher. The acceptance adapter in #2605 owns request authorization,
continuation authorization, operation settlement, and external publication.
The adapter must adopt this producer's exact result identity and content;
it must not implement another sampler or substitute its own verdict.

## Internal interface and accepted operation

Construct `BetaRuntimeSmokeProducer(journal, client, source, profile)` with the
existing `ManagementOperationStore`, configured beta `WorkerNetworkClient`,
read-only `BetaEvidenceReader`, and operator-configured `SmokeProfile`.
The optional `clock` argument is a trusted internal clock boundary used by
focused tests; it is not a caller-supplied MCP parameter.

The owning journal operation has action `run_beta_acceptance`. Its immutable
normalized parameters are:

| Parameter or intent field | Meaning |
| --- | --- |
| `source_sha` | Exact full Git SHA selected for beta |
| `deployment_operation_id` | Exact successful `deploy_beta_source` operation to correlate |
| `supervisor_generation` | Expected Supervisor incarnation |
| `deployment_generation` | Expected deployed worker generation |
| Optional `target_id` | Adapter-owned logical target identifier |
| `expected_incarnation` | Expected worker `process_run_id` |
| `expected_artifact` | Expected installed runtime ID; bootstrap is ineligible |
| Target | Configured beta instance, beta channel, and worker repository |

The original journal operation ID, intent fingerprint, normalized intent,
idempotency binding, and `AcceptedAuthority` are retained. No raw credential,
caller verdict, URL, command, arbitrary runtime path, or selected replacement
candidate is accepted by the producer.

`advance(operation_id)` performs at most one sampling step. The existing
acceptance dispatcher calls it again when the returned `next_sample_at` is due.
There is no internal background task or busy wait. `read(operation_id)` reads
that exact operation's immutable local evidence without consulting Git,
reading the worker, or calling the clock.

`SmokeObservation` separates observation availability from a completed local
verdict:

| `SmokeState` | Meaning |
| --- | --- |
| `NOT_FOUND` | The operation or its initial smoke capture does not exist |
| `IN_PROGRESS` | The original durable window remains unfinished |
| `COMPLETE` | Exact durable local evidence is available |
| `CONFLICT` | Operation or configured target binding does not match |
| `UNAVAILABLE` | Storage or evidence integrity cannot establish a durable answer |

Only `COMPLETE` carries `LocalSmokeEvidence`. An uncertain write cannot be
returned as a durable completed result. A later exact read can establish that
a commit succeeded even when its acknowledgment was lost.

## Source, deployment, and installed-runtime provenance

`BetaEvidenceReader.read_current()` reads the confirmed current beta source
and selection through uncached Git/Actions REST requests. It verifies the
immutable selection history tag, record content, operation identity, and
current selection pointer before returning. It does not discover or select a
newer CI run.

The original tested-source proof and the separately selected source-test
execution are distinct bindings. Each must name the correct repository and
full SHA and provide a successful completed exact run attempt and all required
jobs. The selection's completion time must equal the maximum `completed_at`
of its required jobs. A later successful run or retry at the same SHA can
reuse the unchanged original proof. A newer execution that has not been
selected for beta does not supersede an existing capture.

The producer correlates that exact canonical source evidence with a successful
`deploy_beta_source` operation in the same journal. Both its operation phase
and its persisted lifecycle stage must be successful. Its accepted source
proof and selection, staged source/version/lock/READY identity, new worker
identity, and original Supervisor incarnation must match the acceptance
operation and fresh worker observation. Unresolved deployment, changed source
selection, unavailable evidence, or a candidate mismatch cannot qualify.

The live observation uses a fresh nonce and the existing signed private
Supervisor protocol. The Supervisor supplies its actual selected READY and
launch binding; the same child supplies installed artifact provenance and
lifecycle through its Unix socket. The producer reuses #2603's exact join
between these observations. It records the installed source revision with
`build_embedded` origin, installed distribution version with
`installed_distribution_metadata` origin, installed runtime ID, READY content
identity, locked-input digest, Supervisor incarnation, `process_run_id`, and
worker deployment generation.

The locked-input digest comes from #2610's installed locked environment, joined
by #2600's actual verified launch and the fresh #2599/#2611/#2603 worker
observation. This producer creates no lock receipt or installed-version claim.
Desired SHA, Docker identity, READY alone, and CI success alone do not establish
what is currently running. A different runtime or READY/lock receipt at the
same SHA cannot satisfy the original binding.

## Fixed profile and verdicts

The profile's canonical content determines its immutable `content_revision`.
Defaults are a 180-second total window, a 2-second sampling interval, and two
required advancing samples. Configuration must retain profile ID
`beta-runtime-smoke-v1`, a finite positive interval smaller than the total
window, a finite total window of at most one hour, and a valid required sample
count of at least two. The profile bounds the maximum possible observations to
128 and retains the complete required-predicate set.

Before reading source or worker evidence, the producer persists the original
profile and window opening time/deadline. An absent or invalid profile and an
unavailable clock block sampling. A backward clock observation blocks the
window; expiry does not create a replacement window.

Every qualifying sample requires:

- Successful startup reconciliation and no startup reconciliation error.
- Running lifecycle and `daemon_serving`.
- An open invocation gate and no observed unsettled lifecycle change.
- Authentic worker-produced heartbeat evidence from the same captured
  incarnation, installed source, runtime, READY receipt, and lock.
- Confirmation that the captured exact beta selection is still current.

Ordinary active invocations are not themselves an unsettled lifecycle change.
The sampler does not require that a running worker have no useful work.
Diagnostic reads, nonce responses, elapsed time, and manager-generated changes
are not heartbeat progress. At least two qualifying samples must show strictly
advancing real heartbeat sequence within the original window. Additional
polls observing the same heartbeat cannot create progress.

| `LocalResult` | Authoritative outcome |
| --- | --- |
| `PASS` | The configured count of same-runtime qualifying samples has genuinely advancing heartbeat evidence within the original window |
| `FAIL` | Available worker evidence shows an actual required predicate violation |
| `BLOCKED` | Required evidence or the clock/profile is unavailable, provenance does not qualify, or the original deadline expires without sufficient progress |
| `SUPERSEDED` | A confirmed replacement selection or runtime/worker/Supervisor identity invalidates an unfinished capture |

A different confirmed immutable beta-selection record or beta-operation ID
supersedes an unfinished window even when its SHA, original proof, runtime,
and worker incarnation are unchanged. Missing evidence is not evidence of a
replacement or actual predicate failure. Replacement ends the original
window; it never silently restarts sampling against the replacement.

All four results are local. `FAIL`, `BLOCKED`, and `SUPERSEDED` cannot be
promoted, and local `PASS` alone is not a published acceptance or release
promotion authorization.

## Durable schema and reconstruction

The existing management SQLite database holds a `beta_runtime_smoke` row keyed
by the already admitted operation ID. This is an additive table in the same
journal, not another operation store. Its foreign key, revision-fenced writes,
finite JSON validation, and 1 MiB snapshot limit preserve operation ownership.
It neither changes the operation's phase nor releases its target lane.

Snapshot schema version 1 retains:

| Field | Retained evidence |
| --- | --- |
| `binding` | Original operation ID, intent/fingerprint, accepted authority, and source repository |
| `profile` | Original profile values and immutable content revision |
| `window` | Original opening time and deadline |
| `capture.source` | Exact original tested-source proof, selected execution/jobs, current beta selection and immutable selection identity/content |
| `capture.selection_record_ref` | Exact immutable selection history ref, distinct from the mutable current-selection pointer |
| `capture.deployment` | Successful deployment operation/revision, lifecycle snapshot identity, accepted source evidence, staged READY and launched worker |
| `capture.worker` | Freshly observed installed provenance, runtime/READY/lock and Supervisor/worker incarnation |
| `samples` | Append-only observations, nonces/request IDs, observation times, heartbeat, predicates, installed binding, and qualifying disposition |
| `checks` | Recorded check/result dispositions and safe reason categories |
| `result` | Immutable evidence identity, canonical content, and content SHA-256 |

When a prerequisite is unavailable, the result retains the evidence actually
obtained and explicitly leaves unavailable capture fields absent or null. It
never invents a source proof, runtime binding, sample, or completion timestamp.

The terminal evidence has `schema_version: 1`, record type
`beta-runtime-smoke`, and local identity
`beta-runtime-smoke-v1:<operation_id>`. `content_sha256` binds the exact
canonical JSON content, including operation/source/target identity, original
capture/profile/window, samples/checks, local verdict, reason, and completion
time. Consumers use both the local evidence identity and content digest.
The exact reader verifies the digest and agreement with the persisted capture
before exposing the result.

Reconstructing the manager with the same journal and configured target
continues the same capture and original deadline. A new configured profile
does not replace a captured profile, and refreshed authority does not replace
the original accepted authority. A completed result is returned verbatim
without resampling, even if current Git or worker reads would fail. Different
request input conflicts through the original immutable operation binding.

Original binding/profile/window cannot be rewritten; capture becomes immutable
once assigned, and samples cannot be removed or replaced. A terminal result
freezes the complete snapshot. Identical terminal replay does not increment
its revision. Competing polling calls expose only the committed CAS winner.
Storage corruption, failed writes, or uncertain commit delivery remain
`UNAVAILABLE` until exact readback establishes the durable record.

## Delegated source obligations and production tests

The following mappings preserve the original qualified requirement ownership.
#2605 retains the authenticated adoption/publication boundary and its separate
producer-to-publisher integration with independent publication readback. The
complete producer-to-publisher-to-promotion integration remains owned by
#2606. This implementation does not rename, remove, weaken, or re-own any
#2605 requirement.

| Source obligation retained by #2605 | #2614 implementation responsibility | Verification |
| --- | --- | --- |
| #2605/REQ-002 | #2614/REQ-002: exact original proof, current selection, selected execution/jobs and successful deployment correlation | `tests/test_beta_runtime_smoke.py`; real REST/Git selection and deployment path in `tests/test_beta_runtime_smoke_container.py` |
| #2605/REQ-003 | #2614/REQ-003: actual installed source/version/runtime/READY/lock and worker/Supervisor binding | Same-SHA mismatch and replacement cases in `tests/test_beta_runtime_smoke.py`; actual locked installs, launch and UDS evidence in `tests/test_beta_runtime_smoke_container.py` |
| #2605/REQ-004 | #2614/REQ-004: immutable bounded profile, original window, genuine heartbeat and required predicates | Profile/predicate/clock cases in `tests/test_beta_runtime_smoke.py`; production heartbeat versus held progress in `tests/test_beta_runtime_smoke_container.py` |
| #2605/REQ-005 | #2614/REQ-005: typed local PASS/FAIL/BLOCKED/SUPERSEDED semantics | Verdict, selection and incarnation cases in both producer suites |
| #2605/REQ-006 | #2614/REQ-001 and REQ-006: same acceptance operation, durable capture/result and exact reconstruction | `tests/test_beta_runtime_smoke_store.py`; replay/uncertain-write cases in `tests/test_beta_runtime_smoke.py`; independent process reconstruction in `tests/test_beta_runtime_smoke_container.py` |
| #2605/REQ-011 | #2614/REQ-008: delegated local-producer cases | Real SQLite tests plus `tests/test_beta_runtime_smoke_container.py`; #2605 retains its original consumer/publication integration |
| #2605/REQ-012 | #2614/REQ-009: local producer interface, schema, provenance, recovery and nonpublication documentation | This fragment and the explicitly mapped production tests |

`tests/test_beta_runtime_smoke.py` uses real journal persistence and the actual
source-evidence readers with controlled REST-boundary data. Its explicit
worker double is for focused state and failure tests; it is not the installed
worker acceptance oracle. `tests/test_beta_runtime_smoke_store.py` uses real
SQLite connections, deterministic competing writers, reconstruction, and
controlled persistence failures.

`tests/test_beta_runtime_smoke_container.py`, marked `supervisor_integration`,
uses independently running Supervisor/network/worker Unix-socket processes,
non-editable installations at two actual source revisions, actual locked READY
receipts, and production heartbeat progression. Its external orchestrator
owns isolated Docker fixtures and controlled fault injection; the producer
receives only journal and read clients. The `Supervisor Integration Tests`
workflow executes this boundary. A helper-only PASS or a skipped/unavailable
container run does not establish its acceptance result.

Focused local verification uses:

```sh
CI=true bash scripts/test.sh tests/test_beta_runtime_smoke.py tests/test_beta_runtime_smoke_store.py -q
```

The real installed-worker boundary uses the repository's prepared Supervisor
integration image and:

```sh
CI=true bash scripts/test.sh tests/test_beta_runtime_smoke_container.py -m supervisor_integration -q
```

## Effect and observability boundary

The producer makes read requests and writes its existing operation's local
journal evidence. It does not create Git tags/refs/releases, change source
selection, stage packages, control worker lifecycle, operate Docker, or report
`PASS_PUBLISHED`. Only the later authorized #2605 consumer can establish and
verify external publication of this exact record.

Production logs use the existing Loguru file/stdout setup and record bounded
operation IDs, local outcome and evidence identity. This feature is neutral
for the existing Issue/PR processing dashboard: it adds management acceptance
evidence without changing processing origins, business admission, provider
routing, worker trace events, or dashboard event schemas.
