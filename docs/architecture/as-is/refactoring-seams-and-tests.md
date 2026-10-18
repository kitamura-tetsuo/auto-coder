# Adopted repair seams and verification probes

Source snapshot: **`2c5624a`** (2026-10-18 UTC). [Scope and notation](README.md).

This page records the bounded PR-repair seams used in production and separates maintained verification from still-proposed work. It is not a new Issue contract or evidence of live-provider execution.

## Candidate order and bounded completion checks

| Adopted stage | Production boundary | Bounded completion evidence | Deliberately separate work |
| --- | --- | --- | --- |
| Characterize PR entry and progress decisions | Candidate CI filtering and handler conflict ordering are separate gates | Maintained batch and explicit-origin cases record where each stops | Do not reinterpret known-empty conflict admission as CI success or CI-failure-repair authority |
| Extract PR observation-to-action decisions | `pr_progress_policy` selects an existing executor from typed input | Maintained cases preserve next actions and deferrals without moving receipt authority into guessed flags | No provider/slot-store redesign |
| Isolate execution adapters at existing repair boundaries | `pr_repair_dispatch`, `LocalCIRepairExecution` and `BaseUpdateConflictExecutionService` carry selected operations and results | Maintained cases preserve acceptance, uncertainty, ownership, publication and replay behavior | Synchronous CI/conflict repairs are not durable local jobs |
| Reduce scheduler knowledge of PR internals | The engine currently filters some work before PR policy can decide | Demonstrate the selected decision owner and retained wake-up/deadline path | No required event-bus or full polling rewrite |
| Integrate local CI/conflict execution | Production entries invoke the extracted services while existing producers own effects | Local and cloud origins reach result application and new-head validation in controlled integration | No unrelated engine cleanup or asynchronous-local feature claim |

A source-preserving extraction and an intentional bug fix need different expected outcomes. Record a known defect as such; do not freeze it into a permanent correctness requirement. Conversely, do not silently fix it inside a large mechanical move and lose the ability to identify the behavioral change.

## Production-origin verification map

The PR-repair rows below are maintained by the `pr_repair_contract` group; the
Issue implementation and local-job rows remain proposed probes. Use real in-process stores where their
persisted state is the authority, and controlled external boundaries for
provider/GitHub effects. A mock returning a chosen verdict cannot prove that
the real producer establishes that verdict.

| Origin and contested boundary | Scenario to establish | Observable oracle |
| --- | --- | --- |
| Batch candidate collection to PR handler (maintained) | Current-head CI is empty and PR is conflicted; contrast explicit entry | Whether remediation is reached, why work is deferred, and whether a reentry obligation exists |
| Implementation worker to provider entry | G changes after scheduling or after execution begins but before dispatch | No provider entry for stale approval, with retained/retired ownership consistent with the outcome |
| Dispatcher to external provider | Provider accepts but the response is lost; restart before tracking completes | Same operation is reconciled; no second submission and no false non-start fallback |
| Dispatcher to local runner | Worker returns immediately after `LOCAL_ACCEPTED` | Transferred execution remains owned and exactly one applicable job enters the provider |
| Local runner recovery | Prove old runner dead before entry, then separately after positive entry | Only pre-entry case is eligible for first entry; post-entry case does not replay inference blindly |
| Local finalizer to Git/GitHub | Lose push or create-PR response after the effect occurs | Exact remote head/PR is reconciled and associated once; model is not rerun |
| Review repair admission | Explicit-local marker and exact-PR cloud binding coexist | Conflict is reported before either repair route starts |
| Effective review application to merge preflight | Raw PASS coexists with an unresolved accepted finding | No approval/merge authority is inferred from the raw verdict alone |
| Strong audit to review publication | Result accepted, publication still uncertain; process again | Reconcile the accepted effect instead of treating phase text as confirmation |
| PR repair to independent validation (maintained) | Local correction job settles while repaired-head CI or findings remain unresolved | Job settlement does not open the merge gate |
| Merge command to cleanup | Auto-merge registration succeeds but remote PR is still open | No false observed-merge or premature slot retirement |
| Cleanup/restart to slot release | Frozen target differs from new config, or owned dirty work cannot yet be preserved | Intent retains its target and cleanup remains recoverable without wrong-slot release |

For concurrency-sensitive probes, make the contested ordering observable with a barrier/checkpoint. Merely launching two workers is insufficient: a test that accidentally serializes them does not exercise overlapping ownership.

## Existing test entry points and evidence limits

The repository contains the maintained [PR repair inventory][repair-inventory],
[Issue dispatch tests][dispatch-tests], and [local-job handoff tests][job-tests].
The latter two are navigation anchors for the remaining coverage audit, not
proof that every proposed probe above is implemented. Do not use filenames or
helper-level mocks as a substitute for reading the scenario and its assertion
boundary.

The maintained group is controlled integration, not a provider E2E, production-GitHub, or daemon campaign. Git cases use temporary repositories and external services stay controlled. Documentation checks are not substitutes for the production-boundary assertions in that group.

## Remaining boundaries

The typed policy and execution adapters do not make synchronous local repair restart-safe, replace provider receipt stores, or combine publication, independent validation and lifetime-slot retirement. Those facts remain distinct and any future durable local CI/conflict work needs its own contract.

An event-driven rewrite, provider replacement, global persistence migration and hostile-code sandbox are outside these bounded extraction proposals. A real missing capability should receive its own scoped implementation stage rather than being hidden behind a generic "unified lifecycle" interface.

Sources: [engine origins][engine], [Issue dispatch][dispatch], [runner][runner], [Issue finalizer][finalizer], [PR decision/effect paths][pr].

[dispatch-tests]: https://github.com/kitamura-tetsuo/auto-coder/blob/2c5624a/tests/test_issue_dispatch.py
[repair-inventory]: ../../client-features/pr-repair-execution-test-inventory.md
[job-tests]: https://github.com/kitamura-tetsuo/auto-coder/blob/2c5624a/tests/test_local_job_handoff.py
[engine]: https://github.com/kitamura-tetsuo/auto-coder/blob/2c5624a/src/auto_coder/automation_engine.py
[dispatch]: https://github.com/kitamura-tetsuo/auto-coder/blob/2c5624a/src/auto_coder/issue_dispatch.py
[runner]: https://github.com/kitamura-tetsuo/auto-coder/blob/2c5624a/src/auto_coder/local_job_runner.py
[finalizer]: https://github.com/kitamura-tetsuo/auto-coder/blob/2c5624a/src/auto_coder/issue_job_finalizer.py
[pr]: https://github.com/kitamura-tetsuo/auto-coder/blob/2c5624a/src/auto_coder/pr_processor.py
