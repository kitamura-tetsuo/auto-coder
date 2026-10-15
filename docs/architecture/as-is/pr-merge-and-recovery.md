# PR merge effects and ownership retirement

Source snapshot: **`e1da0e87eb7215d870cbaddd4737bcd9bd6a87e8`** (2026-10-15 JST). [Scope and notation](README.md).

## M1. Merge-command acceptance is not observed merge

```mermaid
sequenceDiagram
    participant P as PR processor
    participant A as Current merge authority
    participant G as GitHub merge command/API
    participant D as Durable post-merge intent
    P->>A: Recheck current head, effective findings, threads and review gates
    P->>A: Check provider continuation and source-Issue guards
    P->>A: Acquire applicable publication fence
    P->>A: Recheck accepted-findings merge authority for exact H
    P->>G: Request merge with auto mode and match-head-commit H
    G-->>P: Command accepted or failed
    P->>A: Release publication fence
    alt Command accepted
        P->>D: Record applicable terminal candidate and cleanup intent
        Note over P,G: Auto-merge may only be registered; remote merge can still be pending
    else Failed, stale or uncertain terminal outcome
        P->>D: Retain applicable recovery evidence rather than infer retirement
    end
```

The default-branch terminal command uses squash/auto mode; a parent-branch path uses merge/auto mode. A parent target is tied to the parent PR's head branch, not its base branch. The expected head is passed to the merge command. These are different terminal variants, not one unconditional merge operation.

A local result field such as `merged=True` after command success must not be consumed as proof that every downstream retirement obligation has completed.

## M2. Cleanup is resumable and has its own state

```mermaid
sequenceDiagram
    participant O as Later PR processing or restart recovery
    participant D as Durable cleanup intent
    participant G as Remote PR and branch observations
    participant R as Issue retirement and slot cleanup
    O->>D: Read staged/pending intent and frozen source/target identity
    O->>G: Observe the terminal condition required by this intent
    alt Required terminal evidence is missing or unknown
        O->>D: Retain obligation and applicable retry deadline
    else Terminal evidence is established
        O->>D: Advance applicable intent toward reclamation
        O->>R: Retire associated Issue work and reconcile exact ownership
        alt Retirement and preservation are established
            R->>D: Complete applicable cleanup
        else Retirement, persistence or workspace evidence is incomplete
            R->>D: Keep recoverable intent; do not report full release
        end
    end
```

Cleanup phases include `STAGED`, `AWAITING_MERGED`, `RECLAIMING` and `COMPLETE`. Terminal hints and branch-only variants affect which evidence is required; a branch-only result must not be relabeled as an observed GitHub PR merge. Recovery uses the frozen target instead of silently changing the intent to the latest configuration.

## M3. Slot reclamation is not simply a Git merge

`_merge_pr_branch_into_base_branch` participates in slot reclamation despite its name. The cleanup path must account for associated Issue retirement and preservation of dirty work before releasing the exact owned slot. Unknown retirement or inability to preserve owned work is not successful cleanup.

The important boundaries are: terminal operation requested, terminal evidence observed, retirement settled, owned workspace safely handled, and slot released. See [state and authority](state-and-authority.md). Moving these calls into smaller files without preserving their evidence dependencies would not be a behavior-preserving refactor.

Sources: [terminal commands, intent recording and cleanup replay][pr] (`_handle_pr_merge`, `_record_terminal_candidate`, `_record_post_merge_intent`, `_process_prs`, `_merge_pr_branch_into_base_branch`); [engine reconciliation][engine].

[pr]: https://github.com/kitamura-tetsuo/auto-coder/blob/e1da0e87eb7215d870cbaddd4737bcd9bd6a87e8/src/auto_coder/pr_processor.py
[engine]: https://github.com/kitamura-tetsuo/auto-coder/blob/e1da0e87eb7215d870cbaddd4737bcd9bd6a87e8/src/auto_coder/automation_engine.py
