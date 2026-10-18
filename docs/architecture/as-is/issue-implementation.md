# Issue implementation admission and handoff

Source snapshot: **`0769b84eaabcf2d0c6a80f6c62660bd179003e02`** (2026-10-18 UTC). [Scope and notation](README.md).

## I1. Approval, execution ownership and dispatch are separate boundaries

```mermaid
sequenceDiagram
    participant O as Implementation-stage origin
    participant W as Issue implementation worker
    participant G as Approved generation state
    participant S as Execution and implementation-slot state
    participant D as Issue dispatch coordinator
    participant B as Selected backend adapter
    O->>W: Candidate with Issue identity and generation G
    W->>G: Verify current approved generation
    W->>S: Establish applicable temporary execution
    W->>G: Revalidate immediately before dispatch
    W->>D: Dispatch using current Issue and captured ownership
    D->>D: Exclude tracking parents and check admission
    D->>S: Recover existing owner or admit selected start
    D->>B: Invoke selected adapter when authorized
    alt Durable local job accepted
        B-->>D: LOCAL_ACCEPTED plus retained job identity
        D-->>W: Execution responsibility transferred
    else Remote accepted or submission uncertain
        B-->>D: REMOTE_ACCEPTED or UNCERTAIN_STARTED
        D-->>W: Retained tracking/reconciliation outcome
    else No start, cannot fix or terminal local result
        B-->>D: Explicit dispatch outcome
        D-->>W: Settle or retain according to that outcome
    end
```

An approved specification is not itself proof of a live provider task. A provider receipt is not proof that slot/tracking bookkeeping has completed. Local job acceptance is neither model completion nor PR creation.

## I2. The normal local route is a durable handoff

```mermaid
sequenceDiagram
    participant D as Common dispatcher
    participant F as Local submission fence and ownership checks
    participant J as Local-job repository
    participant R as LocalJobRunner
    participant X as Local executor and finalizer
    D->>F: Validate Issue, generation and slot for this submission
    F->>J: Accept immutable invocation input once
    J-->>D: LOCAL_ACCEPTED and job ID
    D-->>D: Stop backend fallback
    R->>J: Claim applicable pending job for this repository and kind
    R->>X: Execute recorded input, then finalize retained result
    X-->>J: Result and downstream-effect progress
```

The source still contains `_apply_issue_actions_directly`; its existence does not make the synchronous path the normal daemon implementation route. The local adapter built for the common dispatcher accepts a job. Explicit processing can wait for that job without inventing a second invocation.

### Useful distinctions for refactoring

| Boundary | Evidence consumed | What it does not prove |
| --- | --- | --- |
| Specification approval | Current approved generation G | Provider started |
| Execution admission | Current execution and slot ownership | Provider accepted work |
| Local handoff | Durable job identity and immutable input | Model succeeded |
| Remote acceptance | Provider-specific receipt/retained claim | Local bookkeeping settled |
| Publication | Exact result ownership plus reconciled effects | PR merged or slot releasable |

See [ownership](issue-ownership-and-resumption.md), [local finalization](issue-local-execution.md) and [provider outcomes](issue-provider-dispatch.md).

Sources: [implementation worker][worker]; [Issue integration][issue] (`_dispatch_issue_candidates`, `_build_issue_dispatchers`, `_apply_issue_actions_directly`); [coordinator][dispatch]; [local adapter][local].

[worker]: https://github.com/kitamura-tetsuo/auto-coder/blob/0769b84eaabcf2d0c6a80f6c62660bd179003e02/src/auto_coder/issue_implementation_worker.py
[issue]: https://github.com/kitamura-tetsuo/auto-coder/blob/0769b84eaabcf2d0c6a80f6c62660bd179003e02/src/auto_coder/issue_processor.py
[dispatch]: https://github.com/kitamura-tetsuo/auto-coder/blob/0769b84eaabcf2d0c6a80f6c62660bd179003e02/src/auto_coder/issue_dispatch.py
[local]: https://github.com/kitamura-tetsuo/auto-coder/blob/0769b84eaabcf2d0c6a80f6c62660bd179003e02/src/auto_coder/issue_local_job.py
