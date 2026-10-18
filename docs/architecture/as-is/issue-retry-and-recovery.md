# Issue retry and uncertain-operation recovery

Source snapshot: **`0769b84eaabcf2d0c6a80f6c62660bd179003e02`** (2026-10-18 UTC). [Scope and notation](README.md).

## T1. Explicit retry is not an unconditional fresh submission

```mermaid
sequenceDiagram
    participant CLI as Explicit processing
    participant E as AutomationEngine
    participant S as Retry and provider ownership state
    participant D as Issue dispatcher
    CLI->>E: Process Issue with explicit retry/force options
    E->>S: Establish or recover durable retry request identity
    E->>S: Recover existing accepted or uncertain operation first
    alt Existing operation remains accepted or ambiguous
        E->>S: Reconcile its ownership and tracking
        E-->>CLI: Retained/deferred outcome, not a second provider start
    else A new attempt is authorized
        E->>D: Dispatch under current generation and retry ownership
        D-->>E: Authoritative submission outcome
        E->>S: Record outcome and any unfinished bookkeeping
        E-->>CLI: Report accepted, deferred or terminal state
    end
```

`--force` or an explicit retry request must not be diagrammed as bypassing all ownership checks. A durable retry request, the implementation attempt and the provider task binding are different records.

## T2. Acceptance and bookkeeping can finish in different passes

```mermaid
sequenceDiagram
    participant D as Retry dispatch path
    participant J as Retry submission journal
    participant P as Provider
    participant S as Ownership, slot and tracking stores
    D->>J: Retain authorized submission identity
    D->>P: Submit or reconcile that operation
    alt Accepted provider receipt
        P-->>D: Task identity
        D->>J: Retain accepted evidence
        D->>S: Apply matching ownership and tracking projections
        alt All required bookkeeping is established
            D->>J: Mark applicable handoff progress complete
        else Tracking or persistence remains incomplete
            D->>J: Retain accepted-but-incomplete obligation
        end
    else Submission outcome is ambiguous
        D->>J: Retain uncertainty for reconciliation
    else Definitive evidence says no start occurred
        D->>J: Record non-start outcome for admission policy
    end
```

This is a logical sequence, not a claim that all stores commit atomically. Ordinary provider dispatch has provider-specific ordering; do not apply this retry-journal sequence to every Jules/Claude start. See [provider dispatch](issue-provider-dispatch.md).

## T3. Local recovery depends on the entry checkpoint

A proven-dead runner before provider entry can be recovered for entry. After positive provider entry, loss of the runner is not evidence that inference never started. Once a result is recorded, publication recovery resumes downstream effects without running the model again. Unknown liveness retains ownership rather than freeing capacity speculatively.

For remote work, a temporarily missing task/PR observation is not automatically proof that no accepted operation exists. Existing bindings and uncertain receipts must be reconciled before a replacement start. This document does not assert one universal timeout or recovery algorithm across providers.

### Refactoring-sensitive observations

Preserve the original request/attempt/task identity across a lost response. Preserve the difference between accepted-but-untracked and definitely-not-started. Do not release a lifetime slot merely because the current CLI invocation reports Deferred. These distinctions should be characterized at the real recovery origin, not only through a helper returning a chosen Boolean.

Sources: [explicit processing and accepted retry recovery][engine]; [submission/coordinator outcomes][dispatch]; [local job recovery][runner]; [publication replay][finalizer].

[engine]: https://github.com/kitamura-tetsuo/auto-coder/blob/0769b84eaabcf2d0c6a80f6c62660bd179003e02/src/auto_coder/automation_engine.py
[dispatch]: https://github.com/kitamura-tetsuo/auto-coder/blob/0769b84eaabcf2d0c6a80f6c62660bd179003e02/src/auto_coder/issue_dispatch.py
[runner]: https://github.com/kitamura-tetsuo/auto-coder/blob/0769b84eaabcf2d0c6a80f6c62660bd179003e02/src/auto_coder/local_job_runner.py
[finalizer]: https://github.com/kitamura-tetsuo/auto-coder/blob/0769b84eaabcf2d0c6a80f6c62660bd179003e02/src/auto_coder/issue_job_finalizer.py
