# Duration-weighted PR test shards

PR Tests retains the four existing contiguous pytest-split groups and the
original test selection and within-group order. Before starting an attempt,
`scripts/run_pr_test_shard.py` validates the reviewed duration snapshot at
`tests/data/pr-test-durations.json`, floors zero durations to one millisecond,
and writes a deterministic normalized copy in the shard diagnostic directory.
Every permitted attempt uses that same copy; a missing, malformed, negative,
non-finite, duplicate, overflowing, or subsequently changed input fails the
shard rather than silently changing its allocation. The snapshot is not updated
by CI and no attempt stores partial timing data into it.

The accompanying provenance file identifies the producing runs, jobs and
artifacts, revision, versions, selectors, units, normalization and digests. The
current snapshot uses a native baseline for 12,678 exact nodes from the twelve
successful receipts of [Test Source for Runtime run 43370190764, attempt 1](https://github.com/outliner-online/auto-coder/actions/runs/43370190764)
at `3c10d39bb4f43d2c0c08a4f9c51fce8a7e7171af`. Each receipt is validated
against the immutable plan, actual job interval and complete disjoint source
inventory. The baseline sums pytest's native setup, call and teardown durations
for that outer execution. A call not executed after a recorded setup skip is
absent; no duration or PASS is invented for it. Historical profile samples and
nested pytest executions are excluded from the baseline.

For 11,859 exact nodes, complete result-to-result intervals from [PR Tests run
43370190674, attempt 1](https://github.com/outliner-online/auto-coder/actions/runs/43370190674)
at the same revision replace the baseline. These intervals include intervening
teardown, setup and reporting, so the snapshot contains mixed advisory estimates.
An interval is accepted only when both neighboring result lines identify
consecutive nodes in that group's original selected order. The first result,
unmatched or masked IDs, missing result lines and the unobserved tail retain
native values; 819 baseline nodes are unchanged. Groups 1, 2 and 4 use their
first internal attempt. Group 3 uses completed intervals from its second
internal attempt; attempts are not combined or averaged. All resulting values
are floored at one millisecond and rounded to six decimal places.

The native baseline selector is `not opencode_live`. PR keeps its narrower
`not browser and not opencode_live and not muse_live and not supervisor_integration`
selector, so source-only nodes do not become PR tests. Pytest-split ignores
unselected or removed nodes and uses the mean of matched current nodes for
unknown tests, or an equal default if none match. Exact environment-dependent
node IDs are retained; the snapshot does not relabel a measurement from another
Python executable parameter. The positive floor prevents zero-weight boundary
overflow. Source bootstrap can consume these advisory estimates but does not
treat them as native test evidence. Updates require review and fresh partition
checks.

This refresh addresses an observed imbalance in that PR run. Its old weights
estimated about 347 seconds for every group, while groups 1, 2 and 4 completed
in 492, 366 and 525 seconds. Group 3 continued passing tests until both internal
attempts reached the existing 660-second absolute bound; the second stopped
after 1,291 of its 1,901 selected test results. Its incomplete tail is censored
evidence, not zero-duration work or a passing shard.

Applying the refreshed weights to the same 12,510-node PR collection gives the
following advisory allocation. One local node contains a different Python
executable path from the producing runner and therefore uses the existing
unknown-node mean. The real pytest-split partition check verifies the complete
node multiset, disjoint groups and original within-group order.

| Group | Previous nodes | Refreshed nodes | Refreshed mixed estimate, seconds |
| --- | ---: | ---: | ---: |
| 1 | 5,428 | 5,744 | 539 |
| 2 | 2,042 | 2,330 | 492 |
| 3 | 1,901 | 1,882 | 492 |
| 4 | 3,139 | 2,554 | 444 |

The mixed basis was chosen after comparing a native-only candidate, whose
second group still contained about 586 seconds of observed PR intervals before
startup. Adding the largest observed startup across all five internal attempts
(about 63 seconds) to the refreshed largest estimate gives about 602 seconds.
This comparison is not a future upper bound: unobserved PR work retains source
estimates, timestamp buffering can move costs between adjacent nodes, and
post-result reporting remains outside these intervals. The source producer also
prepares a Python-only management image and Chromium and enables timing
diagnostics. New PR CI must demonstrate completion under its actual setup. A
balanced estimate or a successful partition check does not establish a passing
run.

The shard supervisor retains its existing attempt deadline, bounded progress
extension, idle limit, timeout-only retry policy, process-group cleanup and
failure propagation. Balancing does not change the suite, assertions, markers,
check identities, or workflow timeout.
