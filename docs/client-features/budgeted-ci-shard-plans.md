# Complete time-budgeted CI shard plans

`scripts/plan_ci_shards.py` is an **opt-in offline planner**; the companion
`scripts/ci_shard_plugin.py` is an explicit pytest inventory/application adapter.
They do not change the existing four-way PR/source workflows, the supervised PR
runner, retries, test assertions, markers, fixtures, test/result authority,
required checks, or publication. The planner never authorizes PASS, and no
changed-file-based test selection or result reuse is performed.

## Profile context and real collection

Create a `ci-shard-context-v1` JSON file using the actual `git rev-parse HEAD`
of the tested checkout (not GitHub's often different pull-request event SHA).
Tracked working-tree changes are rejected because `HEAD` would no longer
identify all tested bytes; untracked diagnostic artifacts do not alter `HEAD`.
All arguments in `pytest_args` are **complete and exact**: including targets,
markers, `-k`, configuration options and explicitly enabled other plugins.
For example, the source-test consumer uses `-m 'not opencode_live'`, while the
ordinary PR consumer has a different selector. Do not silently substitute one
for the other.

```json
{
  "schema_version": "ci-shard-context-v1",
  "repository": "kitamura-tetsuo/auto-coder",
  "profile_id": "source-required-v1",
  "checkout_sha": "1111111111111111111111111111111111111111",
  "pytest_args": ["-m", "not opencode_live"],
  "harness_files": ["tests/conftest.py", "tests/ci_timing_plugin.py"],
  "semantic_environment": ["AUTO_CODER_REQUIRE_BROWSER", "AUTO_CODER_IMMEDIATE_TEST_FAILURES"],
  "origin": {
    "repository": "kitamura-tetsuo/auto-coder",
    "workflow": "Test Source for Runtime",
    "run_id": "41246415733",
    "run_attempt": "1",
    "planning_attempt": "source-plan-1",
    "event_sha": "2222222222222222222222222222222222222222"
  }
}
```

The caller explicitly identifies each tracked harness file affecting collection
or execution and every semantic environment variable. The adapter always adds
`PYTEST_ADDOPTS`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD` and `PYTEST_PLUGINS` to the
fingerprint. It also hashes the bytes of `uv.lock` and `pyproject.toml`, the
effective pytest configuration-file path and root directory, exact original pytest arguments,
Python major/minor and the enumerated harness contents. The result is the
profile's `configuration_digest`. Changed source SHA, selectors, configuration,
effective environment or harness refuses plan application; different source
revisions may still use compatible duration history if their configuration
digest is unchanged. Use the digest written into the generated plan as the
configuration identity when producing the optional native duration profile.

```bash
uv run --locked python scripts/plan_ci_shards.py \
  --context source-context.json \
  --duration-profile previous-native-profile.json \
  --output source-plan.json \
  --log-dir ci-plan-logs -- -m 'not opencode_live'

uv run --locked python -m pytest -p scripts.ci_shard_plugin \
  --ci-plan=source-plan.json --ci-shard=1 -m 'not opencode_live'
```

The final `--` and subsequent pytest arguments must match `pytest_args` exactly.
Use attached adapter controls (`--ci-context=PATH`, `--ci-collect-output=PATH`,
`--ci-plan=PATH`, `--ci-shard=NUMBER`) rather than separated values. pytest 9
performs initial root/config discovery before registering third-party options;
separated paths can otherwise become unintended collection targets. The adapter
rejects separated controls instead of accepting an altered profile. The actual
bytes of the effective config file, including an untracked `-c pytest.ini`, are
fingerprinted to detect outcome-affecting changes before test execution.
Only adapter controls (`--ci-*`, `--collect-only`) and explicitly recognized
reporting flags (`-q`, `-v`/`-vv`/`-vvv`, `--junitxml`, `--junit-xml`, `--color`)
are excluded from that semantic argument comparison; other flags stay binding.
If storing context and plan files outside the checkout changes pytest's inferred
root directory, pass the same explicit `--rootdir` in `pytest_args` on both
invocations so node IDs and configuration identity remain stable.
For collection, the planner invokes actual `python -m pytest` with
`--collect-only`, explicitly loads the adapter, and receives versioned JSON
written by the adapter after all standard selectors and collection hooks.
It does not scrape terminal text or read inventory from historical profiles.
Collection failure, duplicates, incorrect checkout/profile/configuration or an
empty required selection refuse publication. Every plan is written atomically
to a **new** file, with exclusive final-name creation and no input overwrite.

## Immutable plan schema and packing

The canonical compact, sorted-key, UTF-8 `ci-shard-plan-v1` JSON has three
top-level parts:

- `semantic`: context (without origin), exact tested checkout SHA, computed
  `profile` (`repository`, `profile_id`, `configuration_digest`,
  `python_major_minor`), ordered complete `inventory` and its SHA-256 digest;
  aligned node `estimates` (`estimated_ms`, `sample_count`, `provenance`),
  `duration_source`, `budget_ms`, `shard_cap`, `effective_capacity_ms`,
  `capacity_limited`, `predicted_total_ms`, and contiguous `shards` (one-based
  `id`, exact `nodeids`, `estimated_ms`, `over_budget_nodes`).
- `digest_sha256`: SHA-256 of the canonical semantic JSON plus one newline.
- `origin`: repository, producing workflow, run, run attempt, planning attempt
  and event SHA (each optional except repository). Origin is **not** in the
  semantic digest, so another planning attempt can identify the same plan.

Native `ci-duration-profile-v1` history is used only if the full immutable
snapshot validates and its repository, profile ID, configuration digest and
Python major/minor match. Obsolete nodes are ignored. Every unmeasured current
node receives a positive estimate of `max(1 second, maximum P95 of measured
current nodes)`; with no measured current nodes every estimate is 1 second.
Fallback provenance is explicitly labeled. Missing, corrupt, stale or foreign
history is diagnostic and **never changes the required inventory**.

Durations are rounded **up** to integer milliseconds, with at least one
millisecond. The ordered inventory is greedily packed into the minimum number
of nonempty contiguous groups whose estimated work fits the 420,000 ms target,
when feasible. If that needs more than 12 groups, the smallest larger
effective capacity fitting 12 is found by integer binary search. Single nodes
over 420 seconds remain indivisible and fully assigned, even for an 820-second
or longer estimate; those nodes are identified separately. Capacity saturation
and predicted shard/test-work totals are visible, and no test is clipped,
omitted or given an artificial deadline.

## Fail-closed application and responsibility

The opt-in pytest application mode reads and verifies the entire canonical
plan at configuration time. Before the first test, it confirms actual Git
checkout and fingerprint, complete post-selector inventory, disjoint original-
order assignments, selected shard ID and unchanged plan bytes. It applies only
the plan's exact member set and checks final selected items again immediately
before entering pytest's native test loop. Subsequent changes to the plan file
fail execution; the adapter never replans after seeing results. It prints the
plan digest and selected node IDs diagnostically, but writes no success receipt.
Native skipped, xfailed, failed, and interrupt outcomes retain their pytest
exit status and artifact semantics.

The current-run source workflow and native retained Supervisor consumers have
**different** run-attempt evidence and result-gate policies. Both must enforce
their own origin/attempt rules and aggregate the actual native results of every
required shard; a stored plan or timing snapshot alone proves no successful
test. The planner does not schedule runners, install Chromium, guarantee queue
availability, shorten process startup, set test timeouts, or promise a 420-second
wall-clock completion. Estimated work ignores variance, fixture overlap,
worker setup, queue delay, matrix overhead and cancellation behavior.

Console diagnostics use colored loguru; `ci-shard-plan.log` under `--log-dir`
rotates at 10 MB with five retained backups. No GitHub credentials or mutable
duration-history service are needed.
