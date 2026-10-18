# Auto-Coder: as-is architecture

Source snapshot: **`007e97cb8be80ccc98e8c4a2604b3b084c23d156`** (2026-10-18 UTC).

These documents describe the inspected production code, not a target architecture or an implementation contract. All source links are pinned to the snapshot above. The diagrams deliberately include blocked, uncertain and partially completed paths; a drawn path is not evidence that a live provider integration succeeds.

## Reading map

| Question | Document |
| --- | --- |
| How do startup, candidate selection and workers interact? | [Engine and Issue lanes](engine-and-issues.md) |
| What admits an Issue implementation? | [Issue implementation](issue-implementation.md) |
| How does local execution reach a published PR? | [Local execution and finalization](issue-local-execution.md) |
| Which ownership survives a worker return? | [Ownership and resumption](issue-ownership-and-resumption.md) |
| How are providers selected and fallback bounded? | [Provider dispatch](issue-provider-dispatch.md) |
| How are retries and uncertain submissions recovered? | [Retry and recovery](issue-retry-and-recovery.md) |
| Where can CI prevent repair from being reached? | [CI and repair](pr-ci-and-repair.md) |
| What does review acceptance actually authorize? | [Adversarial validation](pr-adversarial-validation.md) |
| How do strong audit, closure and publication interact? | [Two-tier review](pr-two-tier-review.md) |
| When are merge effects and slot release complete? | [Merge and recovery](pr-merge-and-recovery.md) |
| Which modules currently own each responsibility? | [Responsibility map](responsibility-map.md) |
| Which facts are authoritative, and which are not? | [State and authority](state-and-authority.md) |
| Which repair paths use local jobs or cloud continuation? | [Backend routing matrix](backend-routing-matrix.md) |
| Where can refactoring start, and what should be characterized? | [Refactoring seams and test probes](refactoring-seams-and-tests.md) |

## Scope and notation

`H` is the captured PR head SHA; `B` is the base identity used by the relevant gate. `G` denotes the approved Issue implementation generation. Attempt IDs, local-job incarnations and review rounds are different identities, not aliases for `G`. In the two-tier review document, `M` and `P` denote the gate's contract and policy identities.

Participants are responsibility groupings, not necessarily separate processes. Arrows show selected calls, durable writes or external effects; adjacent arrows do **not** imply one transaction. A return to a worker does not imply that a provider, publication or lifetime slot has finished. Diagrams omit unrelated logging and presentation details and do not replace individual branch predicates in source.

## Material changes from the previous snapshot

The normal Issue path now sends implementation-lane, refill, explicit-target, and stale-session recovery entries through `IssueImplementationService`. Ordinary provider selection is assembled by `OrdinaryIssueAdapterComposition`, while startup and accepted-retry/local-job recovery are assembled once per repository by `IssueRuntimeComposition`. Local Issue implementation and local PR review correction can transfer execution to durable jobs; local CI repair and conflict repair remain separate.

Explicit-local review routing does not simply override every cloud association: an exact-PR cloud binding plus an explicit-local marker produces a conflict. `KNOWN_EMPTY` CI evidence remains non-passing, but candidate filtering admits an affirmatively conflicting PR when remediation is enabled so the handler can reach conflict repair. Other empty-CI candidates still wait. These are current observations, not fixes introduced by this documentation update.

Model completion, job settlement, PR publication, effective review acceptance, observed merge and slot release remain distinct boundaries. In particular, accepted findings can prevent approval despite a raw reviewer PASS, and a successful `gh pr merge --auto` command need not mean GitHub has already merged the PR.

## Evidence and maintenance

This refresh is based on static inspection at revision `007e97cb8be80ccc98e8c4a2604b3b084c23d156` and the maintained focused integration groups. Controlled integration coverage is not live production-GitHub, paid-provider, or operational deployment evidence; test probes in the refactoring document remain proposals unless explicitly identified as maintained tests.

When refreshing a page, update its source snapshot, recheck its callers as well as its helpers, and retain the distinction between observed behavior and proposed changes. Do not repin old line numbers mechanically. Check both the daemon origin and explicit processing when reachability differs. Source symbols are listed beside pinned file links so they remain searchable without misleading line anchors.

The default project scope is accidental workspace/result corruption within trusted deployment boundaries, not hostile-code confinement. A prompt instruction, private working clone or removed environment variable is not documented here as universal publication prevention.

Sources: [scheduler and entry points][engine], [Issue dispatch][dispatch], [local job state][jobs], [PR orchestration][pr], [CI evidence conversion][ci].

[engine]: https://github.com/kitamura-tetsuo/auto-coder/blob/007e97cb8be80ccc98e8c4a2604b3b084c23d156/src/auto_coder/automation_engine.py
[dispatch]: https://github.com/kitamura-tetsuo/auto-coder/blob/007e97cb8be80ccc98e8c4a2604b3b084c23d156/src/auto_coder/issue_dispatch.py
[jobs]: https://github.com/kitamura-tetsuo/auto-coder/blob/007e97cb8be80ccc98e8c4a2604b3b084c23d156/src/auto_coder/local_job_handoff.py
[pr]: https://github.com/kitamura-tetsuo/auto-coder/blob/007e97cb8be80ccc98e8c4a2604b3b084c23d156/src/auto_coder/pr_processor.py
[ci]: https://github.com/kitamura-tetsuo/auto-coder/blob/007e97cb8be80ccc98e8c4a2604b3b084c23d156/src/auto_coder/util/github_action.py
