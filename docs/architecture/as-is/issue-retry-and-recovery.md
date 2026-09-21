# Issue retry and recovery: as-is sequences

Snapshot: **`d704b54e59ea559cce17d5cfe1a0841ac3ef465d`**. [Issue lifecycle and identities](issue-implementation.md). A fresh operator retry, reentry for the same retained request, and ordinary wake/restart are different origins.

## T1. Explicit retry acquires new authority without deleting old starts

Entry: `process_single` with explicit Issue target, `force` and `retry`. Each invocation creates a request ID; internal reentry carries that same ID. A new CLI invocation is not automatically a replay of the previous request.

```mermaid
sequenceDiagram
    participant U as Explicit process_single
    participant E as Shared Issue admission
    participant G as GitHub
    participant R as IssueStageRoutingStore
    participant S as Implementation slots
    participant H as Reserved dispatcher
    U->>U: Allocate retry request R
    U->>G: Resolve actual target and preflight relationships
    U->>E: Candidate with only, force, retry and R
    E->>E: Run current Issue specification/submission gates
    break Target is a container or current authority is refused
        E-->>U: Refusal, no retry implementation
    end
    E->>G: Final current-generation check under repository authority
    E->>R: Check for another pending request
    break Another request is pending
        E-->>U: DEFERRED, do not acquire this retry
    end
    E->>R: accept_retry_request(R, repository, I, G)
    R-->>E: Durable pending request with distinct attempt A
    Note over E,S: acquire_explicit_retry serializes owner
    E->>R: claim_retry_acquisition(R, I, G)
    alt Request already owned or invalidated
        R-->>E: Retained request status
    else Acquisition still pending
        E->>S: Look for exact retained R/A/G acquisition
        alt Matching acquisition exists
            S-->>E: Existing ownership reference x
            E->>R: Mark R owned using recovered x
        else No matching acquisition
            E->>S: start_execution bound to G, R and A
            S-->>E: x or contention/capacity refusal
            alt x exists
                E->>R: Preserve G owned-start and mark R owned by x
            else Execution was refused
                E->>R: Retain pending request with acquisition refusal
            end
        end
    end
    break No owned execution
        E-->>U: No provider launch
    end
    E->>S: Bind validation identity V
    break Binding fails
        E->>S: Finish execution x
        E-->>U: Return error without provider launch
    end
    E->>H: Dispatch with owned R/A/G/x (T2)
    H-->>E: Reserved processing result
    E-->>U: Result with retry request/attempt diagnostic
```

The slot's retry-acquisition reference recovers the gap between slot persistence and `mark_retry_owned`; the retry request is not treated as consumed merely because a caller started to handle it. Existing generation tombstones remain historical. `bypass_capacity=explicit_only` is passed, but the slot start can still refuse execution contention or hierarchy admission. No reset/delete of old provider records is part of T1. [Explicit entry][explicit] [Admission fork][admission] [Ownership acquisition][ownership]

The diagram's final bind refusal represents the following code ordering: the engine attempts `record_validation_identity`; on failure it finishes the execution and returns. The helper's replay branch for a retained owned request does not by itself prove a new provider task was accepted. For Codex, the reserved boundary consumes the exact request-scoped receipt and confirms its CloudRun, current binding, and logical-slot membership; it does not infer acceptance from a changed binding.

## T2. Reentry for the same owned request does not mean another create

Entry: a provider/local helper receiving an owned `ImplementationRetryRequest`. This describes the actual request-scoped replay mechanism, not an assertion that the daemon automatically replays every unfinished request through this helper.

```mermaid
sequenceDiagram
    participant H as Local/provider launcher
    participant D as RetryDispatchRepository
    participant P as Local invocation or provider
    participant C as Tracking projection
    H->>D: claim(owned R/A/G, route, backend, base configuration)
    alt New record or definitely-not-started outcome
        D-->>H: Creation ID and may_create=true
        Note over H,D: C1: claimed is suppressing after an interruption
        H->>P: Run local helper or create remote task/session
        P-->>H: Result or classified failure
        H->>D: Record accepted/completed, indeterminate or definitely-not-started
        opt Remote accepted result has applicable tracking to project
            H->>C: Persist provider/run/binding tracking
            H->>D: Mark tracking complete
        end
    else Accepted or completed record
        D-->>H: Retained identity and result
        alt Remote route
            H->>C: Repair applicable tracking without external create
            H->>D: Mark tracking complete if successful
        else Local completed record with action checkpoint
            H-->>H: Reuse retained actions without another editing invocation
        end
    else Claimed or indeterminate record
        D-->>H: may_create=false
        H-->>H: Defer without replacement invocation
    end
    H->>D: Controller acknowledges joined run/pointer/slot bookkeeping
```

The journal validates repository, Issue, request, attempt and generation. A suppressing record also binds route/backend/configuration; incompatible reentry raises conflict rather than silently switching providers. Only `definitely-not-started` permits reacquisition and can update the route while preserving the creation ID. Accepted receipts cannot be changed into “unsent.” [Journal][journal]

Provider-specific details remain different:

| Route | Actual retry behavior |
| --- | --- |
| Local | Claims before the local helper; a returned helper result is saved as completed with actions and the ownership reference. This is not proof that a PR exists or tests passed. Escaping errors can record indeterminate. |
| Jules / Claude Routine | Record accepted session identity before tracking. Their `is_latest_accepted` guard prevents older accepted requests from replacing a later accepted current pointer. Jules retry does not enter speculative fan-out. |
| Codex Cloud | Allocates one numeric attempt above observed attempts, projects it through attempt machinery, and additionally uses the CloudRun claim. An accepted retry receipt can reconstruct the run and binding; `ensure_binding` can still refuse contradictory tracking. |

Codex provider tracking is not the enclosing controller checkpoint. An
accepted receipt remains discoverable until the exact CloudRun and current
binding are confirmed and its task is durably present in the Issue's logical
slot (or retained retirement history). The daemon registers these records in
normal pending work at startup and retries only this local bookkeeping. This
recovery retains the original provenance and neither calls the provider nor
allocates a request or numeric attempt. Contention leaves the obligation due
for a later turn in the same daemon; a newer accepted claim makes an older
receipt historical.

These are not one universal exactly-once protocol. The replay diagram concerns the same `R`; it does not suppress every later operator-authorized `R2`. Claims surviving a pre-send crash can intentionally leave creation unresolved rather than authorize an unsafe resend. [Local retry wrapper][local] [Session and Codex retry paths][launchers]

## T3. Stale Jules replacement waits for confirmed termination

Entry: maintenance `handle_stale_jules_issue_sessions` for a mapped, non-stopped session older than the configured no-PR timeout. It checks both session output and GitHub-linked PR evidence before considering replacement.

```mermaid
sequenceDiagram
    participant M as Stale-session maintenance
    participant J as Jules
    participant G as GitHub
    participant S as Implementation slots
    participant A as Fresh dispatch authorization
    participant L as High-score local helper
    M->>J: List sessions and select overdue no-PR candidate
    M->>G: Confirm actual Issue type, open submission and no linked PR
    M->>S: Serialize owner and capture retained generation G
    M->>J: Send stop after retired-session reuse guard
    M->>J: Read session state again
    break Stop failed or state is not COMPLETED/FAILED
        M-->>M: Keep capacity, do not start replacement
    end
    M->>M: Mark session stopped and try diagnostic comment
    M->>S: Finish old local executions and provider membership
    Note over M,S: Legacy no-membership case calls release
    M->>A: Re-fetch and authorize after external stop
    break New submission not authorized or graceful drain active
        M-->>M: No replacement, stopping the old session is not undone
    end
    M->>S: Start replacement execution carrying captured G
    break Execution admission refused
        M-->>M: Defer replacement
    end
    M->>M: Try numeric attempt increment
    M->>L: _take_issue_actions with high-score manager or default
    L-->>M: Local action list
    M->>S: finish_execution in finally
```

The stop helper checks the returned terminal state, not just successful message delivery. It intentionally does not use the full ordinary outbound-work admission path for a stop; it does still guard retired-session reuse. After termination, reauthorization may read changed Issue text while the replacement carries the captured implementation generation. The numeric attempt increment is attempted separately, and an increment error is logged without unconditionally preventing the following local call. These details are preserved rather than normalized into a hypothetical single retry transaction. [Stop and stale selection][stale] [Replacement][replacement]

## T4. Accepted Codex task to initial PR handoff

Entry: `CodexPRRecoveryMonitor`, started separately by the daemon. It selects coherent accepted runs with task/backend/environment/base identities and the latest numeric attempt for that Issue. Default per-task polling is 60 seconds; completion grace is 120 seconds.

```mermaid
sequenceDiagram
    participant M as Codex PR recovery monitor
    participant R as CloudRunRepository
    participant O as Codex observation service
    participant D as Recovery store
    participant W as WHAM follow-up client
    participant E as Durable PR invalidation
    M->>R: Select due coherent accepted runs
    M->>O: Observe Issue, execution and PR presence
    M->>D: Read retained recovery record
    alt Current PR positively observed
        opt PR_OBSERVED is not already retained
            M->>D: Retain PR_OBSERVED and PR identity
        end
        opt Handoff incomplete and shutdown not requested
            M->>E: Invalidate observed PR
            opt Invalidation accepted
                M->>D: Mark handoff complete
            end
        end
    else PR was previously published
        M->>D: Retain PR_OBSERVED if needed, no current-PR handoff
    else Read failed, Issue is not open or PR absence is uncertain
        M->>M: Wait without a reminder
    else Reminder already reserved/sent or a terminal recovery state is retained
        M->>M: Observe retained follow-up or wait, no second automatic POST
    else Open Issue, definite no-matching-PR and eligible completion
        M->>D: Retain completion turn and grace anchor
        Note over M,D: First observation or unexpired grace does not send
        opt Same completion survives grace
            M->>O: Fresh pre-send observation
            break Issue/PR/activity changed or evidence unavailable
                M-->>M: Keep observing without a reminder
            end
            M->>W: Local follow-up preflight
            break Preflight refuses
                M-->>M: No reservation or external follow-up
            end
            M->>D: Reserve sole automatic reminder budget
            break Reservation refused
                M-->>M: No external follow-up
            end
            M->>W: send_follow_up(task, assistant_turn, Create PR)
            W-->>M: Delivered, not-delivered or indeterminate
            M->>D: Retain classified result
        end
    else No recovery-eligible completion
        M->>M: Keep observing, reset old completion grace when applicable
    end
```

PR handoff happens only for PR_PRESENT, not merely PREVIOUSLY_PUBLISHED evidence. A retained handoff can be retried without a new implementation task. After a reminder reservation, subsequent observations do not spend another automatic POST budget; a later completion with causally new user/assistant turns but still no PR can lead to attention-needed state. A rejected/unknown send is not collapsed into an ordinary new implementation retry. The monitor cannot recover an initial submission that has no known task ID by guessing one. [Monitor and durable send reservation][recovery]

[explicit]: https://github.com/kitamura-tetsuo/auto-coder/blob/d704b54e59ea559cce17d5cfe1a0841ac3ef465d/src/auto_coder/automation_engine.py#L6800-L6935
[admission]: https://github.com/kitamura-tetsuo/auto-coder/blob/d704b54e59ea559cce17d5cfe1a0841ac3ef465d/src/auto_coder/automation_engine.py#L5980-L6155
[ownership]: https://github.com/kitamura-tetsuo/auto-coder/blob/d704b54e59ea559cce17d5cfe1a0841ac3ef465d/src/auto_coder/implementation_ownership.py
[reserved]: https://github.com/kitamura-tetsuo/auto-coder/blob/d704b54e59ea559cce17d5cfe1a0841ac3ef465d/src/auto_coder/automation_engine.py#L6290-L6415
[journal]: https://github.com/kitamura-tetsuo/auto-coder/blob/d704b54e59ea559cce17d5cfe1a0841ac3ef465d/src/auto_coder/retry_dispatch.py
[local]: https://github.com/kitamura-tetsuo/auto-coder/blob/d704b54e59ea559cce17d5cfe1a0841ac3ef465d/src/auto_coder/issue_processor.py#L105-L210
[launchers]: https://github.com/kitamura-tetsuo/auto-coder/blob/d704b54e59ea559cce17d5cfe1a0841ac3ef465d/src/auto_coder/issue_processor.py#L267-L850
[stale]: https://github.com/kitamura-tetsuo/auto-coder/blob/d704b54e59ea559cce17d5cfe1a0841ac3ef465d/src/auto_coder/issue_processor.py#L1117-L1350
[replacement]: https://github.com/kitamura-tetsuo/auto-coder/blob/d704b54e59ea559cce17d5cfe1a0841ac3ef465d/src/auto_coder/issue_processor.py#L1350-L1412
[recovery]: https://github.com/kitamura-tetsuo/auto-coder/blob/d704b54e59ea559cce17d5cfe1a0841ac3ef465d/src/auto_coder/codex_pr_recovery.py
