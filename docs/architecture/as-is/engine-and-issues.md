# Engine and Issue admission: as-is sequences

Snapshot: **`95ff379d2bd69e54a53f14c208eedaca5a58e3df`**. [Scope and notation](README.md).

## E1. Startup recovery precedes ordinary workers

Entry: `AutomationEngine.start_automation`. The pending-work and merge-operation schedulers are distinct; a startup GitHub operational failure is recoverable through the already-running pending-work scheduler.

```mermaid
sequenceDiagram
    participant CLI as Daemon entry
    participant E as AutomationEngine
    participant D as Routing and invalidation stores
    participant P as PendingWorkScheduler
    participant M as MergeOperationScheduler
    participant S as Implementation slots
    participant G as GitHub
    participant W as Issue and PR workers
    CLI->>E: start_automation(repository, concurrency)
    E->>D: Recover routing and invalidations, enqueue retained work
    E->>P: Start scheduler and register stage handlers
    E->>M: Start scheduler and register merge resume handler
    Note over E,M: Claude follow-up recovery is also started here
    E->>S: Reconcile open PR ownership, recover reclamation obligations
    E->>G: Strict complete open-entity enumeration
    alt Enumeration and reconciliation succeed
        E->>D: Invalidate targets from enumeration and retained routing/adjudication
        E->>E: Recover accepted Issue review reruns
        E->>P: Supersede any old startup obligation
    else Startup GitHubRequestError
        E->>P: Persist startup obligation and wake scheduler
        E->>E: Wait for startup-ready or shutdown
        P->>E: Retry complete startup reconciliation
        Note over E,G: Retry repeats ownership discovery, enumeration and invalidations
        opt Retry succeeds
            E->>E: Set startup-ready event
        end
    end
    opt Startup recovery completes without shutdown
        E->>E: Start maintenance, invalidation and capacity-refill loops
        Note over E,W: Codex initial-PR recovery starts separately when available
        E->>W: Start separate Issue and PR worker pools
    end
```

The successful enumeration invalidates entities while it runs; the dirty-target arrow summarizes those calls, not an extra scan. An incomplete enumeration does not set `startup_reconciled=True`. Retained routing/adjudication targets absent from the open enumeration are also reevaluated. Other startup exceptions can propagate instead of becoming this retry path.

Sources: [startup and handler registration][startup], [enumeration and maintenance][enumeration].

## E2. Invalidation is a request to observe, not permission to act

Entry: an already-retained entity invalidation, from startup, webhook intake, or another wake producer. HTTP webhook authentication and payload-to-target mapping are outside this sequence. CI SHA correlation and CI-watch deadlines are promoted by the invalidation loop before worker intake.

```mermaid
sequenceDiagram
    participant L as Invalidation loop
    participant D as DurableInvalidationQueue
    participant Q as Candidate queue
    participant W as Worker
    participant G as GitHub
    participant E as Shared candidate processor
    participant S as Ownership reclamation
    L->>D: Promote due CI batches/watches and claim ready work
    L->>Q: Enqueue candidate with invalidation generation g
    Q-->>W: Dequeue candidate
    W->>D: begin_processing(entity, g)
    break Claim is no longer admitted
        W-->>Q: task_done, no entity processing
    end
    W->>G: Create candidate from current authoritative state
    alt Authoritative state says PR is closed
        W->>D: Retire CI watches
        W->>S: Schedule owner reclamation check
        Note over W,S: Scheduling a check does not immediately release a slot
    else Open and admitted candidate
        opt Issue target
            W->>E: Route Review/Implementation stages and family validation
        end
        W->>E: _process_single_candidate(current candidate)
        E-->>W: Outcome, error and optional retry deadline
        opt Issue target
            W->>E: Refresh durable stage routing after processing
        end
    end
    alt PR deferred with a review retry deadline
        W->>D: Persist defer(g, deadline, pr-review-cycle)
        W->>L: Wake deadline consumer
    else Decision completed
        W->>D: complete(claim g)
    else Failure without a committed deferral
        W->>D: release(claim g)
        W->>L: Arrange a 60-second retry wake
    end
    W-->>Q: task_done
```

Issue admission-cache refusal and observed dependency-wait fast paths can run before the strict candidate fetch; the diagram expands the path that reaches that fetch. A strict-refresh operational deferral has its own durable `defer` path. If recording that deferral fails, the worker stops without claiming successful completion; this is not the ordinary release/retry branch above. Draining can stop before dispatch while leaving durable ownership recoverable.

Sources: [CI promotion and invalidation loop][enumeration], [worker refresh, closed-PR handling and timed deferral][worker], [completion/release][worker-finally].

## E3. Issue specification and family admission

This sequence expands the new-admission path that reaches the specification gates, not retained-owner continuation or manual retry. Relationship reconciliation can mutate GitHub parent metadata before semantic validation; it is not merely a local parser. A submitted parent with direct children is treated as a family rather than assumed to be a standalone implementation target.

```mermaid
sequenceDiagram
    participant E as Shared Issue admission
    participant G as GitHub
    participant R as Review lane and validation lifecycles
    participant D as Durable validation decisions
    participant A as Ownership admission (E4)
    E->>G: Strict Issue snapshot and authoritative relationships
    break Invalid relationship or unavailable authority
        E-->>E: BLOCKED or DEFERRED, no implementation dispatch
    end
    alt Submitted parent or inherited parent readiness
        E->>G: Read authoritative parent and complete direct-child set
        E->>R: Schedule enabled decomposition and individual child jobs
        R->>D: Retain exact-identity decisions
        R-->>E: Join decomposition and child results
        Note over E,R: Set validation precedes closed-child filtering and owner routing
    else Independently submitted standalone Issue
        E->>E: Check open/readiness and parse numbered Requirements
        E->>R: Pump Review lane for exact current identity
        R->>D: Verify reusable decision or retain new decision
        R-->>E: READY, BLOCKED, ERROR or no usable decision
    end
    alt Missing or ERROR decision
        E-->>E: Defer, preserve readiness for retry
    else BLOCKED decision
        E->>G: Apply blocked effects only after current-submission authorization
        E-->>E: Stop before implementation
    else Required decisions permit proceeding
        E->>G: Strict dispatch snapshot after awaited review
        E->>G: Reconcile sibling dependencies and recheck family membership
        break Invalid, open or unavailable prerequisite
            E-->>E: BLOCKED or DEFERRED, no slot admission
        end
        E->>E: Compare current identity with reviewed submission identity
        break Edited, withdrawn or reparented submission
            E-->>E: Skip stale generation
        end
        E->>A: Continue with the authorized snapshot
    end
```

The ownership continuation is for an implementable child or standalone Issue, not a parent used as a family container. Parent-to-child routing reenters shared admission; it is not expanded into a parent implementation here.

Semantic validation can be disabled by its configuration switches; this does not remove the manifest, readiness, relationship or final freshness gates illustrated here. An invalid manifest has a separate strict-comment-read/deduplicated diagnostic path and cannot dispatch. A stored terminal decision is not reused merely because it says `READY`: the Review lane reestablishes its identity/evidence applicability. Family ERROR/BLOCKED outcomes are evaluated before implementation eligibility is granted.

Sources: [shared entry and submitted-parent handling][admission], [Review lane reuse][review-lane], [readiness, manifest, semantic verdict and sibling gates][specification], [final identity and membership checks][ownership].

## E4. A local execution ending is not ownership ending

Continuation from E3 for a new ordinary Issue execution. PR candidates share ownership admission but have their own policy gates. Provider internals are intentionally represented by their real entrypoint helper, not by a fictional single remote API.

```mermaid
sequenceDiagram
    participant E as AutomationEngine
    participant G as GitHub
    participant S as ImplementationSlotRepository
    participant H as Issue processing helpers
    participant C as CloudManager bindings
    E->>E: Check graceful-drain boundary
    E->>S: Resolve logical owner
    Note over E,S: Enter repository_dispatch_authority
    E->>G: Recheck generation, readiness and applicable family/dependencies
    E->>S: Start or reuse implementation execution for generation
    opt No slot and not explicit-only
        E->>S: Reconcile capacity
        E->>G: Recheck generation again
        E->>S: Retry atomic admission
    end
    alt Generation already has a durable production start
        E-->>E: SKIPPED, no provider dispatch
    else No execution admitted
        E-->>E: DEFERRED for active execution or capacity
    else execution_id admitted
        E->>S: Bind validated Issue identity
        Note over E,S: Ordinary dispatch is serialized for this logical owner
        E->>G: Reserved boundary confirms actual item type is Issue
        alt difficult label
            E->>H: _process_issue_high_score_cloud
        else jules_mode enabled
            E->>H: _process_issue_cloud_backend
        else Local mode
            E->>H: _take_issue_actions
        end
        H-->>E: Local launcher returns
        E->>C: Read authoritative provider binding
        opt Provider binding exists
            E->>S: Record provider task/session membership
        end
        E->>S: finish_execution(execution_id) in finally
        E->>S: Reconcile ownership against current evidence
    end
```

`--only` bypasses capacity in this path; forced PR recovery has a separate active-execution bypass. That is not blanket permission to bypass Issue semantic generation checks. The normal Issue path can still refuse a generation with a durable start. Explicit `--only --force --retry` follows additional durable retry-authority handling, not expanded here.

A failed provider launch marked `CloudSubmissionNotStartedError` permits unbound-idle cleanup only after the code confirms both no CloudRun and no binding. An unreadable provider store retains ownership instead. A closed-PR observation schedules fresh reclamation checks; the capacity-refill loop consumes those checks and can then admit other Issues. Neither `finish_execution` nor a terminal PR alone is depicted as unconditional slot release.

Sources: [final freshness, admission and cleanup][ownership], [reserved dispatch and binding mirror][dispatch], [refill and reclamation consumer][refill].

[startup]: https://github.com/kitamura-tetsuo/auto-coder/blob/95ff379d2bd69e54a53f14c208eedaca5a58e3df/src/auto_coder/automation_engine.py#L2467-L2685
[enumeration]: https://github.com/kitamura-tetsuo/auto-coder/blob/95ff379d2bd69e54a53f14c208eedaca5a58e3df/src/auto_coder/automation_engine.py#L2680-L2900
[worker]: https://github.com/kitamura-tetsuo/auto-coder/blob/95ff379d2bd69e54a53f14c208eedaca5a58e3df/src/auto_coder/automation_engine.py#L3580-L3810
[worker-finally]: https://github.com/kitamura-tetsuo/auto-coder/blob/95ff379d2bd69e54a53f14c208eedaca5a58e3df/src/auto_coder/automation_engine.py#L3810-L3832
[admission]: https://github.com/kitamura-tetsuo/auto-coder/blob/95ff379d2bd69e54a53f14c208eedaca5a58e3df/src/auto_coder/automation_engine.py#L5132-L5350
[review-lane]: https://github.com/kitamura-tetsuo/auto-coder/blob/95ff379d2bd69e54a53f14c208eedaca5a58e3df/src/auto_coder/automation_engine.py#L3363-L3420
[specification]: https://github.com/kitamura-tetsuo/auto-coder/blob/95ff379d2bd69e54a53f14c208eedaca5a58e3df/src/auto_coder/automation_engine.py#L5600-L5840
[ownership]: https://github.com/kitamura-tetsuo/auto-coder/blob/95ff379d2bd69e54a53f14c208eedaca5a58e3df/src/auto_coder/automation_engine.py#L5840-L6110
[dispatch]: https://github.com/kitamura-tetsuo/auto-coder/blob/95ff379d2bd69e54a53f14c208eedaca5a58e3df/src/auto_coder/automation_engine.py#L6110-L6350
[refill]: https://github.com/kitamura-tetsuo/auto-coder/blob/95ff379d2bd69e54a53f14c208eedaca5a58e3df/src/auto_coder/automation_engine.py#L3420-L3580
