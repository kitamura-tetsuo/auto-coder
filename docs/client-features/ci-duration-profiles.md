# Rolling native CI duration profiles

`scripts/update_ci_duration_profile.py` materializes optional, immutable
test-duration estimates from the existing native pytest timing recorder. It is
an offline diagnostic consumer, not a pytest plugin, selector, test-success
authority, workflow change, or GitHub client. It does not write
`tests/data/pr-test-durations.json` or its reviewed provenance.

The complete source workflow now consumes these snapshots as **optional
estimates**, with source-profile identity validation, unique run/attempt cache
keys and explicit manifests for each actual source shard. A missing, corrupt
or incompatible cache leaves a complete plan with labeled positive fallbacks.
Timing updates and artifact uploads remain diagnostic only; source success
still requires exact current-plan, native job and receipt qualification.

## Inputs and origin adapter

The CLI requires one explicit profile context, any number of `--batch` JSON
manifests and `--previous` immutable snapshots, a *new* `--output` destination,
and `--log-dir` for rotating loguru diagnostics:

```bash
uv run --locked python scripts/update_ci_duration_profile.py \
  --context profile-context.json \
  --batch run-41246415733.json \
  --previous prior-profile.json \
  --output profile-2026-11-08.json \
  --log-dir timing-profile-logs
```

The `ci-duration-context-v1` JSON schema has the following shape. The caller
sets the evaluation time explicitly, which also becomes snapshot generation
time; this makes replay reproducible.

```json
{
  "schema_version": "ci-duration-context-v1",
  "profile": {
    "repository": "kitamura-tetsuo/auto-coder",
    "profile_id": "pr-tests-native",
    "configuration_digest": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "python_major_minor": "3.12"
  },
  "evaluation_time_utc": "2026-11-08T00:00:00Z"
}
```

The `ci-duration-batch-v1` manifest declares the **same profile object**,
plus `files` (one or more JSONL paths relative to the manifest location) and
one explicit `origin`:

```json
{
  "schema_version": "ci-duration-batch-v1",
  "profile": {
    "repository": "kitamura-tetsuo/auto-coder",
    "profile_id": "pr-tests-native",
    "configuration_digest": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "python_major_minor": "3.12"
  },
  "origin": {
    "kind": "github-actions",
    "checkout_sha": "1111111111111111111111111111111111111111",
    "event_sha": "2222222222222222222222222222222222222222",
    "workflow": "PR Tests",
    "run_id": "41246415733",
    "run_attempt": "1",
    "job_key": "pytest-shard-1",
    "invocation_id": "explicit-pytest-invocation"
  },
  "files": ["timing/timing-invocation-process.jsonl"]
}
```

This is an explicit **batch-origin adapter**. Its fields must match the
corresponding native `context` in *every* accepted observation. The actual
`git rev-parse HEAD` checkout SHA is the `checkout_sha`; `GITHUB_SHA`
(`event_sha`) is not evidence of the tested checkout and cannot replace it.
The native process UUID, PID and patch-level Python version are retained from
each JSONL record, so multiple process files can belong to one invocation.

For a local batch, set `kind` to `local` and set `event_sha`, `workflow`,
`run_id`, `run_attempt` and `job_key` to `null`. Keep the real checkout and
invocation ID. Partial Actions provenance is not silently converted to local
provenance. Never synthesize missing Actions fields from filenames or event
metadata. The configuration digest is the caller's stable SHA-256 over the
test configuration; the CLI deliberately does not guess which fixture/selector
configuration was used. A different configuration or Python major/minor uses
a different profile.

## Sample and snapshot semantics

Only an exact parametrized node ID with **matching native phase start and
report records for setup, call, and teardown** supplies a sample. The
sample is the sum of the three native pytest `native_duration_ms` values,
converted to seconds. Nested harness span durations, log intervals, job queue
time and inferred waits never contribute. Positive finite measurements over
300, 600 or 820 seconds are retained without clipping. Failed, xfailed and
teardown-failed executions with all three reports can contribute timing, while
their actual phase outcomes remain present. Setup failure, skip-before-call,
interruption or missing/conflicting phase records do not produce a sample or
an inferred zero. In repeated executions of the same node within one process,
incomplete and conflicting phase starts still establish execution boundaries:
a later call or teardown cannot complete an earlier setup. Unambiguous,
independently complete executions remain eligible. No snapshot field
authorizes a successful test result.

The output schema is `ci-duration-profile-v1`. It includes:

* `profile`, `generated_at`, `schema_version` and `digest_sha256` (SHA-256 of
  the canonical sorted-key compact UTF-8 JSON **without** `digest_sha256`, with
  one trailing newline);
* `retention_days` (14), `max_samples_per_node` (20), and
  `min_estimate_seconds` (0.001);
* `status`: `available`, `partial` or `unavailable`, with `reasons` and
  per-input `inputs` diagnostics (type, source, status, count, reasons);
* `nodes`, keyed by exact node ID. Each node has `p95_seconds`, `sample_count`,
  `oldest_at`, `newest_at` and retained `samples`. Each sample includes a stable
  digest-based `sample_id`, its observed UTC time, actual batch/process origin,
  all three phase span IDs and UTC start times, native phase durations and phase outcomes (including
  expected-failure metadata), `raw_duration_seconds` and positive
  `duration_seconds`.

The estimate is nearest-rank P95: sort retained positive sample durations and
select the value at `ceil(0.95 * N)`. A genuine zero native sum is retained in
`raw_duration_seconds` while the positive estimate representation uses a
0.001-second floor. Newest-first age/20-count retention is applied *after*
deduplication by stable execution/sample identity; timestamps and sample IDs
provide deterministic tie-breaking. Repeated identical delivery records count
once. Contradictory new deliveries with the same sample identity are omitted;
a conflicting batch cannot overwrite previously validated historical samples.
History from older checkout revisions within the same repository/profile,
configuration digest and Python major/minor can contribute, but foreign
profiles, invocations or Actions origins cannot be relabeled.

The input manifests and previous snapshots are read-only. An output must not
already exist; it is built in a same-directory temporary file, flushed,
fsynced and published by an atomic same-filesystem hard link with exclusive
final-name creation. A concurrent publisher never replaces the first winner;
the losing invocation exits nonzero and removes only its private temporary
file. A failed output/context operation exits nonzero and does not alter
previous snapshots or native pytest status. Same normalized inputs
and evaluation time generate identical output bytes.

## Diagnostics and fallback

Malformed lines, foreign origins, incomplete phases and unusable historical
snapshots have explicit reasons. An imported partial snapshot retains its
diagnostic qualification. A profile with samples plus such losses is
`partial`; a profile without retained complete samples is `unavailable`. An
unknown or expired node has **no `nodes` entry**, never an invented zero or
success. The CLI logs diagnostics to colored console output and a rotating
`ci-duration-profile.log` file under `--log-dir`; it does not log raw records,
long tracebacks or credentials. Invalid input history does not evict valid
samples imported from another snapshot.

The caller/downstream system owns collecting CI artifacts and persisting each
new immutable snapshot. This feature does not store global history, upload to
GitHub, change CI required checks, or wire a scheduling algorithm. Consumers
may use `nodes[exact_nodeid].p95_seconds` as an *optional measured estimate*
and must retain their own unknown-node fallback, without treating a duration
profile as a test-result cache.
