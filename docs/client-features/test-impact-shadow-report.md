# Retrospective test-impact shadow reporting

Issue #2796 extends the manual, advisory [test-impact observation](test-impact-observation.md)
with an **optional baseline comparison**. This is an evidence-gathering tool, not a
CI optimization or permission to omit tests. The complete ordinary PR pytest
profile is still executed exactly once on the evaluated current checkout, with
native setup/call/teardown outcomes and the original pytest exit status. No
mandatory workflow, PR comment, promotion, repair or merge behavior uses this
report; neither comparison nor baseline data changes test invocation.

## Manual use

1. Dispatch **Test Impact Observation (Advisory)** with a full `source_ref`
   commit SHA. Wait for a **successful and complete** run; record both its exact
   GitHub Actions run ID and attempt number. Its `observation.json` artifact is
   immutable and uniquely named `test-impact-<run>-<attempt>-<invocation>`.
2. Dispatch the **same workflow** again with a newer descendant full `source_ref`
   SHA, and optionally fill in **both** `baseline_run_id` and
   `baseline_run_attempt`. For example:

   ```bash
   gh workflow run test-impact-observation.yml --ref main \
     -f source_ref=<current-full-commit-SHA> \
     -f baseline_run_id=<baseline-run-id> \
     -f baseline_run_attempt=<baseline-attempt-number>
   ```

   The dispatch `--ref` selects the workflow definition; `source_ref` is the
   **actual evaluated checkout**, which may differ from the workflow event SHA.
   Use a source revision that already includes the shadow-report script.
3. Inspect the job summary and `shadow-report.json` / `shadow-report.md` in the
   current invocation's uniquely named artifact. They are generated **after**
   the full native pytest run and independent normalizer. A failed current test
   can still have a complete diagnostic observation and MISSED_FAILURE report;
   neither report masks the native failure. If the current run is interrupted,
   its result is NOT_VALIDATED instead.

Without either baseline input the original observation-only path runs unchanged,
without artifact download, candidate computation or report. A single missing
input, absent/expired/corrupt artifact or failed download produces an explicit
unavailable report if the current invocation can run the reporting CLI, never an
empty valid selection. The workflow requires read-only `actions` and `contents`
permissions; the checkout does not persist credentials. There is no new
scheduled trigger or required status check.

The maintained CLI also accepts already recorded observations for local
retrospective use (without any GitHub API call):

```bash
uv run python -m scripts.report_test_impact \
  --baseline BASE.json --current CURRENT.json \
  --repository-dir CHECKOUT \
  --output REPORT.json --summary REPORT.md
```

Workflow invocation uses the directory-based artifact input and additionally
verifies the requested exact repository, workflow, run and **attempt** through
GitHub's read-only run-attempt REST API. It accepts only an authoritative
completed, successful baseline workflow attempt, matches the downloaded
artifact's own run/attempt/invocation identity and rejects ambiguous multiple
artifacts. The workflow verifies the API provenance **before downloading** and
rechecks before accepting the report; it also checks the downloaded raw F
manifest, native exit, collection events and coverage context database against
the normalized artifact. No "latest", newer retry, another repository or PR partial diff is
substituted. A locally supplied JSON artifact without remote/raw verification is useful
for reproduction but is not independently authenticated GitHub-run evidence.

## Candidate scope and conservative fallbacks

Only these changes permit a historical candidate:

- An `M`-modified, already **baseline-observed**
  `src/auto_coder/**/*.py` file, with a valid baseline source fingerprint,
  fully usable baseline node/phase mapping and no unattributed execution on the
  changed source. Select exactly its **baseline** file-to-node edges that still
  exist in the current collected inventory.
- Changed `tests/test_*.py` (also nested under `tests/`) files: select every
  current node from those exact files. Add all newly collected exact node IDs
  and any surviving baseline node with unusable/no phase coverage mapping.
  Parametrization IDs remain intact; baseline nodes removed from collection are
  reported separately, not silently retained as executable current tests.

The comparison verifies the actual checked-out baseline and current commit/tree
objects and ancestor relation, and parses the **whole** NUL-delimited Git
path-status interval between them. A valid no-change interval has an empty
change list, but it is distinct from missing/truncated Git evidence. Source
additions/deletions/renames, missing/unattributed/unmapped source relationships,
`conftest.py` or shared fixtures, dependency lock/config/runner/workflow changes,
non-Python resources and all other unmeasured changed domains trigger `FULL`
with explicit reasons. Mismatched tool/lock/observer versions or schema, bad
attempt or repository identity, nonancestor/unknown Git objects, malformed
coverage or incomplete collection instead trigger an unavailable fallback:
`FULL` names every known currently collected node; `UNKNOWN` states that the
complete profile is required without inventing node IDs if collection cannot
be established.

The observation's permanent `unmeasured_domains` catalog lists fundamental
limitations (subprocess, Docker, resources, environment, unobserved runtime
paths, unsupported/native execution). These capability caveats are always
reported. A change in one of those domains forces `FULL`; the presence of the
catalog alone does not imply that such a change occurred. An apparently narrow
Python candidate is **retrospective empirical evidence only**, never a proof
that all possible indirect effects were measured.

## Report and native failure overlay

`shadow-report.json` uses deterministic keys and ordered arrays with:

- Exact baseline and current run/attempt/invocation, evaluated commit/tree and
  observation profile; availability, native session, collection digest and
  measurement-tool fingerprints of both; verified compared interval and each
  original path-status record (including both rename paths).
- `comparison_status` (`candidate`, `full`, `unavailable`), `selection_mode`
  (`BASELINE_CANDIDATE`, `FULL`, `UNKNOWN`), reasons, unmeasured changed paths
  and the observer's unmeasured-domain catalog.
- Exact frozen `candidate_nodes`, `omitted_nodes`, `new_nodes`,
  `removed_nodes` and whether the current collected inventory is complete.
- `validation_status` (`missed_failure`, `no_observed_miss`, `not_validated`)
  and a `MISSED_FAILURE` record for each genuinely failed/error native current
  node hypothetically omitted by the **already frozen** baseline candidate.
  Current-run coverage edges never retroactively add a node to that candidate.
- `omitted_native_outcomes` preserving each omitted node's actual phase state,
  outcome and `wasxfail` metadata, without equating a skip/xfail with PASS.
- Current native phase-time sums for full, selected and omitted nodes, **not**
  CI wall-clock, queue or startup time, and never a guaranteed time saving.

Pytest's native `failed` outcomes in setup/call/teardown count as failures.
Native `skipped`/xfail/XPASS metadata is retained in the original observation;
an xfail or skip is not converted into a successful executed test in the report.
An incomplete current native invocation, non-equivalent baseline/current
measurement or FULL/UNKNOWN fallback yields `NOT_VALIDATED`, not a claim of zero
missed failures. The complete ordinary profile remains mandatory regardless of
what this retrospective calculation shows.

Actual test omission, automatic rollout criteria, performance thresholds and
evidence of safe production test selection are **deferred to a separate user
decision**. Neither this implementation nor a green advisory run grants those
authorities.
