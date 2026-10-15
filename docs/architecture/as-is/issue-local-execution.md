# Local Issue execution and PR publication

Source snapshot: **`e1da0e87eb7215d870cbaddd4737bcd9bd6a87e8`** (2026-10-15 JST). [Scope and notation](README.md).

## L1. Model execution records a result in an invocation-owned clone

```mermaid
sequenceDiagram
    participant R as LocalJobRunner
    participant J as Local-job repository
    participant X as Issue local executor
    participant S as Current Issue, generation and slot evidence
    participant M as Configured local execution backend
    R->>J: Claim job and record runner ownership
    R->>X: Execute immutable job input
    X->>S: Recheck Issue openness, G, execution and source ownership
    X->>X: Prepare private clone from captured slot source branch
    X->>S: Revalidate workspace and ownership at provider entry
    alt Entry cannot be authorized
        X-->>R: Deferred or non-success evidence without a new model call
    else Entry authorized
        X->>J: Record positive provider-entry checkpoint
        X->>M: Run captured prompt and backend selection
        M-->>X: Completion, cannot-fix or failure evidence
        X->>J: Record outcome and owned workspace/artifact evidence
    end
    R->>J: Continue downstream processing when a result is available
```

The working repository lives below `job_workspaces` and uses a job/incarnation-specific branch. Its base is tied to the slot's source branch, not whichever HEAD happens to be checked out in the controller. The executor captures Git-visible result evidence. This path does not establish a universal requirement that every project test has passed.

The prompt asks the model not to publish to GitHub. That instruction and the private clone are not a claim of malicious-agent publication confinement.

## L2. Finalization, not model exit, owns publication completion

```mermaid
sequenceDiagram
    participant R as Runner or publication recovery
    participant F as Issue job finalizer
    participant S as Slot, execution and generation state
    participant J as Local-job effect journal
    participant G as Git remote and GitHub PR API
    R->>F: Finalize recorded artifact without rerunning inference
    F->>S: Check exact Issue, G, execution and incarnation ownership
    F->>F: Verify source, workspace, head and continuation completion
    alt Guard rejects or required evidence is unknown
        F-->>R: Retain pending work or report non-success
    else Already associated result
        F->>S: Reconcile the existing association idempotently
    else Owned result has no changes
        F->>S: Finish applicable temporary execution without a PR
    else Owned publishable result
        F->>J: Record downstream effect intent
        F->>G: Push expected head without force, or reconcile prior push
        G-->>F: Exact remote-head evidence or uncertain result
        F->>G: Find exact-head PR before creating one
        G-->>F: Existing/new PR receipt or uncertain creation
        alt Publication and association can be confirmed
            F->>S: Associate PR only with the same generation and owner
            F->>S: Finish temporary execution
            F->>J: Settle downstream job work
        else Publication remains ambiguous
            F->>J: Retain replay/reconciliation obligation
        end
    end
```

The sequence compresses separate effect checkpoints; it is not one transaction spanning Git, GitHub and local stores. An uncertain push is reconciled against the expected remote head. An uncertain PR creation is not a reason to blindly create another PR. Unknown continuation liveness/completion does not become successful exit by default.

The lifetime implementation slot can remain associated with the PR after this job settles. [Merge cleanup](pr-merge-and-recovery.md), not local model exit, eventually retires that ownership.

Sources: [runner][runner]; [job phases and entry evidence][jobs]; [Issue executor][local]; [publication finalizer][finalizer] (`local_publication_guard` consumption and publication classification).

[runner]: https://github.com/kitamura-tetsuo/auto-coder/blob/e1da0e87eb7215d870cbaddd4737bcd9bd6a87e8/src/auto_coder/local_job_runner.py
[jobs]: https://github.com/kitamura-tetsuo/auto-coder/blob/e1da0e87eb7215d870cbaddd4737bcd9bd6a87e8/src/auto_coder/local_job_handoff.py
[local]: https://github.com/kitamura-tetsuo/auto-coder/blob/e1da0e87eb7215d870cbaddd4737bcd9bd6a87e8/src/auto_coder/issue_local_job.py
[finalizer]: https://github.com/kitamura-tetsuo/auto-coder/blob/e1da0e87eb7215d870cbaddd4737bcd9bd6a87e8/src/auto_coder/issue_job_finalizer.py
