# Live local PR repair smoke

This opt-in pilot records real GitHub evidence for the existing synchronous
local CI-repair and conflict-repair routes. It is not part of ordinary CI and
its deterministic tests are not live-E2E evidence.

## Safety and prerequisites

Use a repository created or explicitly designated as disposable by its owner.
The harness refuses `kitamura-tetsuo/auto-coder` and
`kitamura-tetsuo/outliner`. Clone the disposable repository into a dedicated,
clean workspace; do not point the harness at a workspace containing unrelated
work. Its effective origin fetch and push URLs and the PR's head and base
repositories must all identify the designated repository. Put `--evidence`
outside that clone. Keep normal Actions workflows enabled and configure the selected local
backend as the first ordinary-priority producer in the usual Auto-Coder
configuration. The harness verifies that producer and uses the supported
`process-issues` interface rather than attempting a command-line override.
It pins the resolved configuration path in both child invocations and requires
the fixture's configured test script before starting.
Export `GITHUB_TOKEN` with only the permissions needed for that disposable
repository. No provider credential is written to the repository or evidence file.

The fixture must have a normal pull-request workflow whose test result is
independent of the model. Use fixture-only branches and a unique prefix such as
`ac-live-smoke-<date>-`; record those branch names before beginning so cleanup
cannot target unrelated refs.

Add `<!-- auto-coder:local-llm -->` to each fixture PR body. This is the
supported local ownership marker used by ordinary CI and conflict repair; a
branch prefix alone is not attribution. The harness blocks before processing
when the marker (or another supported local Auto-Coder identity) is absent.
Both harness invocations explicitly disable optional Codex-auth secret
synchronization, preventing an unrelated configured secret publisher from
mutating another repository.

## CI-failure flow

1. From the disposable repository's base, create a fixture branch containing a
   small genuine source defect and a focused test that fails because of it.
   Include a separate harmless fixture change that remains after the repair so
   the repaired PR is non-empty. Configure the repository test script to select
   that focused test, and ensure a real PR Actions workflow runs it. Push it and
   open a test PR. Wait for the PR's current head (H1) to have a terminal-failed
   GitHub Actions run.
2. From the Auto-Coder checkout, run the harness against the clean clone:

   ```console
   uv run python scripts/live_local_pr_repair_smoke.py ci \
     --repository OWNER/DISPOSABLE --pr 123 --workspace /tmp/ac-smoke-ci \
     --backend codex --evidence /tmp/ac-smoke-ci-evidence.json
   ```

3. The harness invokes ordinary explicit PR processing with the configured
   local backend. It passes only after GitHub reports a different PR head H2
   and a successful pull-request Actions run whose `head_sha` is H2. A zero
   process exit, local test, model response, or push message is insufficient.
   The repair artifact must identify an APPLIED, synchronized H2 and the actual
   local editing invocation. The independent pass must then record a confirmed
   merge of that same head. A generic processing success, closed-empty PR,
   deferred review, or merely clean open PR is not final acceptance.

## Conflict flow (including KNOWN_EMPTY H1)

1. Create a fixture base branch from the repository's normal base. Create a PR
   branch from it and change one fixture line while adding an assertion for the
   PR-side behavior. Push and open a PR targeting the fixture base. Change the
   same line differently on the fixture base and add an independent assertion
   for that behavior, then push it. The final test must require both intended
   changes, not merely the absence of conflict markers. Wait until GitHub
   reports the PR as conflicting. The original head may
   have no checks (`KNOWN_EMPTY`); this state is valid only for affirmative
   conflict repair, not for the CI-failure flow.
2. Run:

   ```console
   uv run python scripts/live_local_pr_repair_smoke.py conflict \
     --repository OWNER/DISPOSABLE --pr 124 --workspace /tmp/ac-smoke-conflict \
     --backend codex --evidence /tmp/ac-smoke-conflict-evidence.json
   ```

3. Inspect the repaired file in the published H2 as well as the evidence. The
   harness selects the existing base-update/conflict route, requires a changed
   remote head, and waits for fresh H2 Actions before reporting `PASSED`.

## Results and reset

Retain the two redacted JSON files with the pilot record. They contain the
repository and PR, backend/origin, H1/H2 and base identities, every pre/post
current-head run ID and their aggregate conclusions, process result, merge
disposition, repair and gate actions, ownership disposition, and failure phase.
The separate `.repair-result.json` and `.gate-result.json` artifacts preserve
the production outcomes that feed that evidence. Missing
credentials, repository, clean clone, failed-H1, or confirmed-conflict state is
reported as `BLOCKED`, never as a pass.

After recording results, close only an unmerged test PR created for the pilot
and delete only its recorded fixture head/base branches. Remove the dedicated
clone. Do not revert, reset, or clean a shared workspace. If either flow fails,
preserve its evidence and open a separate corrective issue rather than changing
backend priority, protections, secrets, workflows, or validation gates. Both
positive observations are required before the durable follow-up begins.

`--timeout` bounds only polling for the published H2 and its Actions result.
The existing synchronous processing subprocess has its ordinary lifetime; this
pilot does not promise cancellation or introduce a process supervisor.

## Requirement evidence status

| Requirement | Retained evidence |
| --- | --- |
| REQ-001 | Supported explicit processing entry, admitted fixture/configuration, and actual editor invocation. |
| REQ-002 | Terminal-failing H1, causally matched published H2, H2 Actions, and matching-head final merge. |
| REQ-003 | Affirmative conflict, actual H1 CI state, semantic fixture assertion, H2, and final gate. |
| REQ-004 | Dedicated clean clone/remotes, same-repository PR, external artifacts, and disabled secret sync. |
| REQ-005 | Redacted target, repair, execution, per-head CI, gate, and phase-specific failure facts. |
| REQ-006 | Both positive live JSON records plus this scoped reset procedure; absent records remain BLOCKED. |
| REQ-007 | Opt-in invocation and deterministic tests that never claim live verification. |

The deterministic suite validates the harness contract only. Until an operator
supplies the disposable repositories, PRs, credentials, connected local
provider, and review configuration and retains both positive records, live
acceptance is **BLOCKED**.
