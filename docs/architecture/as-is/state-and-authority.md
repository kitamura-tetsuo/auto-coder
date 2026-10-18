# State, evidence and authority boundaries

Source snapshot: **`2c5624a`** (2026-10-18 UTC). [Scope and notation](README.md).

## A fact needs a producer, not just a convenient flag

| Question | Authoritative producer/evidence in the inspected path | Consumer | Unsafe substitution |
| --- | --- | --- | --- |
| May this Issue generation start? | Current approved generation and execution/slot admission | Implementation worker and provider-entry checks | Old approved label or captured Issue text alone |
| Did a local invocation enter the provider? | Durable provider-entry checkpoint for this job/incarnation | Runner recovery | Parent process exited or no exception was logged |
| Is the previous runner dead? | Runner identity/liveness evidence, including process identity checks | Recovery and claim handling | Unknown or failed liveness lookup |
| Does this result belong here? | Captured generation, execution, incarnation, source and workspace evidence | Local finalizer | Same Issue number or a directory name |
| Was an external submission accepted? | Provider-specific claim/receipt/reconciliation | Dispatch and retry recovery | Transport exception means nothing started |
| Was this branch/PR published? | Expected remote head and exact PR observation/receipt | Publication finalizer | Model says it pushed, or local commit exists |
| Is current CI usable? | Exact-head Actions/check observations | Candidate filter and PR processor | Empty list means success |
| Did a PR repair start or publish? | `PRRepairResult` attempt, receipt, resulting-head and synchronization facts from the selected executor | PR orchestration and re-entry | Action text, helper return truthiness or a local commit alone |
| Which review correction route is valid? | Explicit markers plus exact-PR binding and current head | Review repair admission | Source-Issue provider history alone |
| Are accepted findings closed? | Durable accepted-findings projection and independent closure evidence | Effective application and final merge preflight | Raw PASS or ADDRESSED reply |
| Is strong review complete? | Applicable H/B/M/P cycle plus accepted/publication state | Two-tier merge gate | STRONG_PENDING text or a completed model call |
| Has the PR merged? | Applicable remote terminal observation | Cleanup/reconciliation | `gh pr merge --auto` returned successfully |
| May the slot be released? | Applicable retirement/cleanup settlement and exact ownership | Slot reclamation | Job SETTLED or worker returned |

## Local-job phase and outcome are orthogonal

The durable phase path includes `PENDING` → `RUNNING` → `RESULT_RECORDED` → `DOWNSTREAM_EFFECTS_PENDING` → `SETTLED`. Recovery can revisit eligible pending work only under its entry/liveness rules; this is not a blanket retry loop.

`COMPLETED`, `CANNOT_FIX`, `FAILED` and `INTERRUPTED` describe outcomes, not the same axis as the phase. A completed model can have unfinished publication. A settled PR correction job can still be awaiting independent validation. A settled Issue publication can leave a lifetime slot associated with its PR.

The default local-job SQLite path is `~/.auto-coder/local_jobs.sqlite3`. Job repository scope, kind, target, upstream attempt and incarnation matter; a global lookup by Issue/PR number would lose these distinctions.

## Identity domains must not be collapsed

| Domain | Relevant identity | Why a fresh observation matters |
| --- | --- | --- |
| Issue implementation | Repository, Issue, G, attempt/execution | Approval or ownership can change between scheduling and entry |
| Local job | Kind, target, upstream identity, job and incarnation | Old process/result evidence can outlive the worker |
| PR repair/review | Repository, PR, H, repair/review attempt | A repaired or independently updated head invalidates old assumptions |
| Two-tier review | H, B, contract M, policy P, round and finding revision | Completion applies to a particular contract and repair scope |
| External effect | Exact operation and its provider/publication identity | Lost responses require reconciliation, not duplicate effects |
| Cleanup | Frozen source/target, associated ownership and retirement state | Current configuration must not silently retarget old cleanup |

## Current joins are not a universal transaction

Generation/slot state, local jobs, provider histories, retry state, review effects and post-merge intents are joined by orchestration and replay. `IssueImplementationService` joins admission to exact execution settlement, while `IssueRuntimeComposition` joins repository startup services; neither replaces those decisive stores. Their co-occurrence in one function does not create atomic commit semantics. Retain explicit unknown and accepted-but-incomplete states when moving code across boundaries.

The PR repair adapter likewise creates no transaction. Its immutable request
binds repository, PR, head, repair kind and the already-established origin;
its result keeps receipt availability, candidate head and synchronization as
separate facts. Reopening a cloud receipt store or returning to the controller
therefore reuses the producer's durable identity rather than manufacturing a
local fallback, and a synchronized new head must still pass fresh CI and
independent review.

This map concerns correctness under the project's existing trusted deployment boundaries. It does not turn prompt instructions or ordinary workspace isolation into proof that an adversarial process cannot publish or escape.

Sources: [implementation worker][worker], [job evidence][jobs], [runner recovery][runner], [dispatch][dispatch], [publication][finalizer], [CI conversion][ci], [PR authority and cleanup][pr], [review cycle][cycle].

[worker]: https://github.com/kitamura-tetsuo/auto-coder/blob/2c5624a/src/auto_coder/issue_implementation_worker.py
[jobs]: https://github.com/kitamura-tetsuo/auto-coder/blob/2c5624a/src/auto_coder/local_job_handoff.py
[runner]: https://github.com/kitamura-tetsuo/auto-coder/blob/2c5624a/src/auto_coder/local_job_runner.py
[dispatch]: https://github.com/kitamura-tetsuo/auto-coder/blob/2c5624a/src/auto_coder/issue_dispatch.py
[finalizer]: https://github.com/kitamura-tetsuo/auto-coder/blob/2c5624a/src/auto_coder/issue_job_finalizer.py
[ci]: https://github.com/kitamura-tetsuo/auto-coder/blob/2c5624a/src/auto_coder/util/github_action.py
[pr]: https://github.com/kitamura-tetsuo/auto-coder/blob/2c5624a/src/auto_coder/pr_processor.py
[cycle]: https://github.com/kitamura-tetsuo/auto-coder/blob/2c5624a/src/auto_coder/pr_review_cycle.py
