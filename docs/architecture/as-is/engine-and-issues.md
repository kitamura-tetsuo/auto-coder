# Engine, startup and Issue lanes

Source snapshot: **`0769b84eaabcf2d0c6a80f6c62660bd179003e02`** (2026-10-18 UTC). [Scope and notation](README.md).

## E1. Startup is a reconciliation barrier, not just worker creation

```mermaid
sequenceDiagram
    participant Caller
    participant E as AutomationEngine
    participant S as Durable stores and GitHub observations
    participant R as PR correction LocalJobRunner
    participant I as Issue implementation LocalJobRunner
    participant W as Candidate workers
    Caller->>E: Start automation for one repository
    E->>E: Require repository-scoped handler context
    E->>R: Start retained PR correction processing
    E->>S: Recover retained work and reconcile slots, open PRs and Issues
    alt Startup reconciliation cannot establish a usable view
        E->>S: Retain work and defer with recovery evidence
        Note over E,I: New Issue execution is not admitted through the barrier
    else Startup barrier completes
        E->>I: Start ISSUE_IMPLEMENTATION jobs with configured capacity
        E->>W: Run PR and Issue processing lanes
    end
```

PR review correction may be resumed before the Issue startup barrier. This is not permission to start new Issue work using an unreconciled slot view. Repository-scoped stores and the global capacity governor are different coordination scopes.

## E2. Candidate discovery can stop work before its handler

```mermaid
sequenceDiagram
    participant Q as Discovery, invalidations and deadlines
    participant E as AutomationEngine
    participant C as CI observation helper
    participant P as PR processor
    participant S as Issue specification worker
    participant I as Issue implementation worker
    Q->>E: Candidate or retained work becomes eligible
    alt Batch PR candidate collection
        E->>C: Check current CI progress
        alt Helper reports in progress, including KNOWN_EMPTY
            E->>E: Exclude candidate before PR handler
        else Candidate survives filters
            E->>P: Process PR
        end
    else Ordinary Issue specification work
        E->>S: Process with skip_implementation=True
        S-->>E: Specification/admission outcome
    else Dedicated implementation-stage work
        E->>I: Process captured approved generation G
        I-->>E: Deferred, retained, accepted or terminal outcome
    end
```

The producer loop also performs maintenance and periodic discovery. It should not be drawn as a single full-repository polling loop that alone owns all scheduling. Retained jobs, retry obligations and deadlines have their own reentry paths. See [CI reachability](pr-ci-and-repair.md).

## E3. The implementation lane does not perform semantic review

```mermaid
sequenceDiagram
    participant W as Issue implementation worker
    participant A as Approved generation evidence
    participant E as Execution ownership
    participant D as Common Issue dispatcher
    W->>A: Is captured G still current and approved?
    alt Stale or unavailable approval
        W-->>W: Remove stale work or retain deferred work as applicable
    else Current approval
        W->>E: Begin or recover the applicable execution
        W->>A: Recheck G before dispatch
        alt Approval changed
            W-->>W: Do not start a provider
        else Still current
            W->>D: Dispatch implementation candidates
            D-->>W: Authoritative dispatch outcome
            Note over W,E: LOCAL_ACCEPTED transfers temporary execution to a durable job
        end
    end
```

Normal dispatch is no longer an engine-level `jules_mode` decision tree. The implementation lane calls the coordinator described in [provider dispatch](issue-provider-dispatch.md). A one-shot CLI can wait for its accepted local job; daemon workers can return. Neither behavior permits the worker's `finally` block to retire execution already transferred to that job.

Sources: [AutomationEngine][engine] (`start_automation`, `_get_candidates`, `_process_single_candidate`, `_process_single_candidate_reserved`); [dedicated implementation worker][worker]; [Issue dispatcher integration][issue] (`_dispatch_issue_candidates`).

[engine]: https://github.com/kitamura-tetsuo/auto-coder/blob/0769b84eaabcf2d0c6a80f6c62660bd179003e02/src/auto_coder/automation_engine.py
[worker]: https://github.com/kitamura-tetsuo/auto-coder/blob/0769b84eaabcf2d0c6a80f6c62660bd179003e02/src/auto_coder/issue_implementation_worker.py
[issue]: https://github.com/kitamura-tetsuo/auto-coder/blob/0769b84eaabcf2d0c6a80f6c62660bd179003e02/src/auto_coder/issue_processor.py
