# Manual Python test-impact observation

Issue #2794 adds a **manual-only, advisory** diagnostic. It does not select,
skip, retry or reorder tests, reduce mandatory CI wall time, change PR comments,
or authorize merging, publication or repair. Ordinary `PR Tests`, `scripts/test.sh`
and local `pytest` commands do not enable coverage contexts or load the impact
observer. Neither this diagnostic nor its job is a required check.

## Manual invocation

In **Actions → Test Impact Observation (Advisory) → Run workflow**, provide a
required `source_ref` (a full commit SHA, tag or branch). The workflow has only
`workflow_dispatch`; there is no automatic run or changed-file selection.
For the GitHub CLI, one example is:

```bash
gh workflow run test-impact-observation.yml --ref main -f source_ref=<full-commit-SHA>
```

`--ref` selects the revision of the *workflow definition* to dispatch; the
`source_ref` input selects the code to check out and test. The observation
records the actual `git rev-parse HEAD` and `HEAD^{tree}` after checkout, in
addition to `GITHUB_SHA`, which identifies the dispatch event and need not
match the tested revision. Use the full SHA for reproducible investigations.

GitHub Actions uses Python 3.12 and `uv sync --locked --extra test --dev`.
One native pytest invocation runs the complete ordinary PR marker profile:

```bash
python -m pytest -p tests.ci_impact_plugin --cov=. --cov-context=test --cov-report= --cov-fail-under=0 \
  -m 'not browser and not opencode_live and not muse_live and not supervisor_integration'
```

The diagnostic does not shard the suite or introduce reruns. Pytest's native
assertions, fixtures, teardown, child-process/container behavior and exit status
remain authoritative. The separate normalization step runs even after a test
failure and cannot turn a failing pytest job green. A diagnostic destination
known to be unusable before execution causes a single uninstrumented run of the
same native selection with an explicit observer-unavailable warning.

## Evidence identity and schema

The upload is named `test-impact-<run>-<attempt>-<invocation>`; every attempt
uses a fresh UUID-owned directory beneath the runner's temporary area. The
retained artifact includes `manifest.json`, `pytest-events-<pid>.jsonl`,
`native-exit.json`, `coverage.db`, `observer.log` when writable, and
`observation.json` when finalization succeeds. Partial raw files are uploaded
even if normalization or native pytest fails. Outputs from a previous invocation
are never used as a fallback.

The version-1 `observation.json` binds together:

- `identity`: repository, requested source ref, actual checkout commit and tree,
  separate event SHA, exact workflow path/event, run, attempt and UUID;
- `profile`: fixed full marker expression, ordered collected parametrized node
  IDs and a SHA-256 digest of their canonical JSON array;
- `tooling`: Python, pytest, pytest-cov, coverage.py and uv versions, and SHA-256
  fingerprints of the lockfile, pytest configuration and observation scripts;
- `native_session`: real pytest exit receipt and session-finalization exit,
  without mapping nonzero native results to PASS;
- `nodes`: each exact parametrized node with setup/call/teardown native outcomes,
  native durations, `wasxfail` metadata and context-specific observed repo
  Python source files;
- `sources`: relative repository `.py` paths with SHA-256 file fingerprints
  and observed coverage contexts;
- `unattributed_execution`: the empty coverage context for collection/import
  and other out-of-test execution; `unmeasured_domains`: explicit limits;
- `status` and `errors`: diagnostic completeness independent of test success.

Pytest-cov context suffixes `|setup`, `|run`, and `|teardown` are parsed **from
the right**, preserving `::` and `|` characters in parametrized node IDs.
No file is assigned to a test based merely on imports, naming or an absent
context. The `sources` entries can include files inspected by coverage without
executed lines; these are not proof of non-dependency.

## Completeness and limitations

The three independent evidence states are:

- `available`: collection, native session completion and exit agree, the
  coverage database is readable, every reported phase can be joined to its
  valid context, and records have one consistent invocation identity. Native
  pytest can still report a genuine test failure.
- `incomplete`: some raw evidence exists but phases, session finalization,
  coverage contexts, source identity, or records are missing, invalid or
  contradictory. Interrupted calls and diagnostic write failures do not create
  fake zero-dependency mappings.
- `unavailable`: the observation cannot establish an invocation with usable
  evidence. Neither `incomplete` nor `unavailable` is a passing test-impact
  index.

Each phase separately distinguishes measured file edges, `measured_empty`
(a real coverage context with no in-repo Python file edge), `no_evidence`
(no corresponding context), and `unreached` (for example a call after a skipped
or failed setup). These are **not interchangeable**. Collection-time imports
are retained in an unattributed context, not silently charged to a test.

Only in-process Python line execution within the repository is measured.
Subprocesses, Docker/container workloads, native code, non-Python files and
resources, environmental/configuration inputs, and dynamically unobserved or
unsupported domains remain **unmeasured**, not independently proven absent.
Consequently this is **not a complete dependency graph** and must not yet be
used to omit tests. A corrupt DB, unknown context or inconsistent checkout
identity prevents an `available` index. A normalizer/write failure is exposed
as a separate failing diagnostic step; the original native pytest failure
remains visible.

The controller excludes the verified exact workflow path
`.github/workflows/test-impact-observation.yml` and associated same-repository
check-run facts from CI waiting, aggregate status and repair input, by path/run
provenance rather than workflow/check names. Unknown or foreign associations
remain actionable. Prompt-regression generated-comment verification is unchanged.

## Observed diagnostic overhead

One local illustrative measurement on Python 3.12 with a warmed `uv --no-sync`
environment used the 15 tests in `tests/test_github_ci_observer.py` and
`tests/test_prompt_regression_advisory_isolation.py`. The uninstrumented
`python -m pytest` wall time was **2.215 s**; with the impact plugin,
`--cov=. --cov-context=test --cov-report= --cov-fail-under=0`, it was
**4.556 s** (an additional **2.341 s**, approximately **106%** for this small
sample). These are single local observations, not a controlled benchmark or
full-suite/hosted-runner estimate. End-to-end overhead can be larger due to
coverage context recording, postprocessing and artifact upload. Neither
mandatory CI execution nor its timing is changed by this opt-in workflow.
