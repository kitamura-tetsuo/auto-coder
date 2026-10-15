# Two-tier PR review and publication

Source snapshot: **`e1da0e87eb7215d870cbaddd4737bcd9bd6a87e8`** (2026-10-15 JST). [Scope and notation](README.md).

## S1. Strong execution and accepted publication are separate

The optional two-tier gate has its own applicability checks and contract/policy identity. It is not a universal extra review for every PR. Where applicable, the gate uses the PR head/base and collected Issue requirement declarations; its contract identity is not a generic synonym for every Issue parser in the system.

```mermaid
sequenceDiagram
    participant P as PR processor
    participant D as PR review-cycle repository
    participant V as Strong reviewer in pinned worktree
    participant E as Review effect executor and authenticated transport
    P->>P: Resolve applicable H, B, contract M and policy P
    P->>D: Record ordinary convergence and inspect cycle
    alt Open strong findings require ordinary closure
        P->>P: Use closure path S2
    else Strong execution is applicable
        P->>D: Claim strong audit for current identity
        alt Claim not admitted or execution unavailable
            P->>D: Retain/defer or abandon applicable claim
        else Claim admitted and reviewer usable
            P->>V: Execute STRONG_AUDIT
            V-->>P: Complete result or incomplete/error evidence
            alt Acceptable complete result
                P->>D: Record accepted strong round
            else Incomplete or failed execution
                P->>D: Abandon claim with applicable retry evidence
            end
        end
    end
    P->>D: Read accepted pending publication
    opt Exact accepted effect remains pending
        P->>E: Send or reconcile exact review effect
        E-->>P: CONFIRMED, REJECTED or UNCERTAIN
        opt Confirmation for the applicable effect
            P->>D: Acknowledge publication and applicable pass completion
        end
    end
    P->>D: Authorize merge for current H, B, M and P
    D-->>P: Applicable completion or retained blocking state
```

An accepted-but-unpublished result is not permission to launch another strong audit indiscriminately. Conversely, merely calling a helper with "pending" in its name does not prove only one execution ever occurs: durable cycle identity, claim and publication state decide that.

## S2. Ordinary closure must account for retained strong findings

```mermaid
sequenceDiagram
    participant P as PR processor
    participant D as PR review-cycle repository
    participant V as Ordinary closure reviewer
    participant E as Exact review publication
    P->>D: Read retained strong round, findings, revision and version
    P->>P: Check retained base, contract and policy applicability
    P->>V: Review cumulative repair diff at current head H2
    Note over D,V: Input includes retained findings and their round/revision
    V-->>P: Verdict, dispositions and scope evidence
    alt Incomplete or non-pass closure
        P-->>P: Keep unresolved findings
    else Complete closure result
        P->>D: Certify closure with captured version and finding identity
        alt Bounded closure accepted
            D-->>P: Accepted closure publication pending
            P->>E: Publish/reconcile exact accepted closure
            E-->>P: Confirmation or pending effect
            P->>D: Acknowledge only when confirmed
        else Expanded or unknown scope accepted
            D-->>P: Renewed strong audit required
        end
    end
```

A raw ordinary PASS does not delete strong findings. Pending-publication reconciliation can still occur when this invocation did not run a new reviewer. `STRONG_PENDING` alone is not a receipt: it can cover execution/retry work or accepted evidence awaiting publication.

Daemon processing can persist a targeted review-cycle deadline for reentry. That is not a promise that a one-shot CLI invocation keeps running until the deadline. The final PR merge path still checks current head, CI, unresolved threads, provider continuation and accepted-findings authority independently of this gate.

Sources: [gate inputs, strong audit, closure and publication callers][pr]; [cycle state][cycle]; [merge predicate][gate]; [deadline reentry][engine].

[pr]: https://github.com/kitamura-tetsuo/auto-coder/blob/e1da0e87eb7215d870cbaddd4737bcd9bd6a87e8/src/auto_coder/pr_processor.py
[cycle]: https://github.com/kitamura-tetsuo/auto-coder/blob/e1da0e87eb7215d870cbaddd4737bcd9bd6a87e8/src/auto_coder/pr_review_cycle.py
[gate]: https://github.com/kitamura-tetsuo/auto-coder/blob/e1da0e87eb7215d870cbaddd4737bcd9bd6a87e8/src/auto_coder/two_tier_pr_gate.py
[engine]: https://github.com/kitamura-tetsuo/auto-coder/blob/e1da0e87eb7215d870cbaddd4737bcd9bd6a87e8/src/auto_coder/automation_engine.py
