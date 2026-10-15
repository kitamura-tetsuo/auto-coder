# Refactoring seams and characterization probes

Source snapshot: **`e1da0e87eb7215d870cbaddd4737bcd9bd6a87e8`** (2026-10-15 JST). [Scope and notation](README.md).

This page separates observed coupling from proposed work. It is not an approved implementation sequence, a new Issue contract or a claim that the listed probes already pass.

## Candidate order and bounded completion checks

| Stage proposal | Why this boundary is relevant now | Bounded completion evidence | Deliberately separate work |
| --- | --- | --- | --- |
| Characterize PR entry and progress decisions | Candidate CI filtering and handler conflict ordering can disagree | Exercise batch and explicit origins against the same captured observations and record where each stops | Fixing the known empty-CI progress defect is a behavior change, not automatically part of a refactor |
| Extract PR observation-to-action decisions | `_handle_pr_merge` combines observation, repair, review and terminal effects | Equivalent next actions and deferrals for preserved cases, without moving receipt authority into guessed flags | No provider/slot-store redesign |
| Isolate execution adapters at existing repair boundaries | Durable local review jobs and synchronous CI/conflict repair have different contracts | Preserve acceptance, uncertainty, ownership and replay behavior per adapter | Do not claim all repairs are asynchronous merely because the interface is shared |
| Reduce scheduler knowledge of PR internals | The engine currently filters some work before PR policy can decide | Demonstrate the selected decision owner and retained wake-up/deadline path | No required event-bus or full polling rewrite |
| Integrate local CI/conflict execution incrementally | The durable local-job substrate already exists, but these repair paths do not uniformly use it | One supported repair origin reaches actual execution, result application and new-head validation | No need to wait for every unrelated engine cleanup |

A source-preserving extraction and an intentional bug fix need different expected outcomes. Record a known defect as such; do not freeze it into a permanent correctness requirement. Conversely, do not silently fix it inside a large mechanical move and lose the ability to identify the behavioral change.

## Production-origin test probes to map before implementation

These are proposed probes, not an assertion about existing test coverage. Use real in-process stores where their persisted state is the authority, and controlled external boundaries for provider/GitHub effects. A mock returning a chosen verdict cannot prove that the real producer establishes that verdict.

| Origin and contested boundary | Scenario to establish | Observable oracle |
| --- | --- | --- |
| Batch candidate collection to PR handler | Current-head CI is empty and PR is conflicted; contrast explicit entry | Whether remediation is reached, why work is deferred, and whether a reentry obligation exists |
| Implementation worker to provider entry | G changes after scheduling or after execution begins but before dispatch | No provider entry for stale approval, with retained/retired ownership consistent with the outcome |
| Dispatcher to external provider | Provider accepts but the response is lost; restart before tracking completes | Same operation is reconciled; no second submission and no false non-start fallback |
| Dispatcher to local runner | Worker returns immediately after `LOCAL_ACCEPTED` | Transferred execution remains owned and exactly one applicable job enters the provider |
| Local runner recovery | Prove old runner dead before entry, then separately after positive entry | Only pre-entry case is eligible for first entry; post-entry case does not replay inference blindly |
| Local finalizer to Git/GitHub | Lose push or create-PR response after the effect occurs | Exact remote head/PR is reconciled and associated once; model is not rerun |
| Review repair admission | Explicit-local marker and exact-PR cloud binding coexist | Conflict is reported before either repair route starts |
| Effective review application to merge preflight | Raw PASS coexists with an unresolved accepted finding | No approval/merge authority is inferred from the raw verdict alone |
| Strong audit to review publication | Result accepted, publication still uncertain; process again | Reconcile the accepted effect instead of treating phase text as confirmation |
| PR repair to independent validation | Local correction job settles while repaired-head CI or findings remain unresolved | Job settlement does not open the merge gate |
| Merge command to cleanup | Auto-merge registration succeeds but remote PR is still open | No false observed-merge or premature slot retirement |
| Cleanup/restart to slot release | Frozen target differs from new config, or owned dirty work cannot yet be preserved | Intent retains its target and cleanup remains recoverable without wrong-slot release |

For concurrency-sensitive probes, make the contested ordering observable with a barrier/checkpoint. Merely launching two workers is insufficient: a test that accidentally serializes them does not exercise overlapping ownership.

## Existing test entry points and evidence limits

The repository contains [Issue dispatch tests][dispatch-tests] and [local-job handoff tests][job-tests]. These are navigation anchors for the next coverage audit, not proof that all probes above are implemented. Do not use filenames or helper-level mocks as a substitute for reading the scenario and its assertion boundary.

This documentation refresh does not report an application test run, provider E2E run or fault-injection campaign. It introduces no runtime behavior change. Markdown/link and diagram-structure checks, when performed for the documentation commit, are not equivalent to rendering Mermaid or validating the production behavior.

## Questions a later implementation preflight must settle

Choose one authority for deciding the next PR action without making that consumer invent CI, ownership or publication evidence. Decide which synchronous repair path is being adapted and what its accepted/unknown/completed outcomes mean. Keep publication, independent validation and lifetime-slot retirement distinct. Existing capabilities should be integrated rather than reimplemented under a larger abstraction.

An event-driven rewrite, provider replacement, global persistence migration and hostile-code sandbox are outside these bounded extraction proposals. A real missing capability should receive its own scoped implementation stage rather than being hidden behind a generic "unified lifecycle" interface.

Sources: [engine origins][engine], [Issue dispatch][dispatch], [runner][runner], [Issue finalizer][finalizer], [PR decision/effect paths][pr].

[dispatch-tests]: https://github.com/kitamura-tetsuo/auto-coder/blob/e1da0e87eb7215d870cbaddd4737bcd9bd6a87e8/tests/test_issue_dispatch.py
[job-tests]: https://github.com/kitamura-tetsuo/auto-coder/blob/e1da0e87eb7215d870cbaddd4737bcd9bd6a87e8/tests/test_local_job_handoff.py
[engine]: https://github.com/kitamura-tetsuo/auto-coder/blob/e1da0e87eb7215d870cbaddd4737bcd9bd6a87e8/src/auto_coder/automation_engine.py
[dispatch]: https://github.com/kitamura-tetsuo/auto-coder/blob/e1da0e87eb7215d870cbaddd4737bcd9bd6a87e8/src/auto_coder/issue_dispatch.py
[runner]: https://github.com/kitamura-tetsuo/auto-coder/blob/e1da0e87eb7215d870cbaddd4737bcd9bd6a87e8/src/auto_coder/local_job_runner.py
[finalizer]: https://github.com/kitamura-tetsuo/auto-coder/blob/e1da0e87eb7215d870cbaddd4737bcd9bd6a87e8/src/auto_coder/issue_job_finalizer.py
[pr]: https://github.com/kitamura-tetsuo/auto-coder/blob/e1da0e87eb7215d870cbaddd4737bcd9bd6a87e8/src/auto_coder/pr_processor.py
