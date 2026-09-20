# Issue ownership, retirement and resumption: as-is sequences

Snapshot: **`d704b54e59ea559cce17d5cfe1a0841ac3ef465d`**. [Issue lifecycle and identities](issue-implementation.md). A completed controller call, a provider's completion, a terminal PR, a released reservation and a historical implementation start are separate facts.

## O1. Launcher return does not mean Issue completion

Entry: the reserved Issue dispatcher returning from L1/P1-P4, with a locally acquired execution rather than an inherited execution.

```mermaid
sequenceDiagram
    participant H as Issue helper
    participant E as Reserved dispatcher and outer processor
    participant C as CloudManager / CloudRun records
    participant S as Implementation slots
    H-->>E: Action list or escaping exception
    alt Helper returns normally
        E->>C: Read current Issue binding
        opt Cloud manual retry
            E->>E: Require a new tracking target versus pre-dispatch binding
        end
        opt Binding exists
            E->>S: Mirror provider task/session membership
        end
        E->>E: Set success and target SUCCESS if checks did not raise
    else CloudSubmissionNotStartedError
        E->>E: Mark deferred and cloud_submission_not_started
    else Retryable backend error or other exception
        E->>E: Retain the corresponding deferred/error result
    end
    Note over E,S: Outer finally for this locally acquired execution
    E->>S: finish_execution(owner, x)
    opt cloud_submission_not_started
        E->>C: Strictly check all Issue CloudRuns and current binding
        alt No runs and no binding confirmed
            E->>S: Attempt release_unbound_idle_owner
        else Existing or unreadable provider evidence
            E->>E: Do not release through this shortcut
        end
    end
    E->>S: reconcile against GitHub
    E-->>E: Return candidate result
```

The normal Issue branch sets success after the helper returns and binding handling succeeds; it does not parse every action string as a failure or verify that remote work completed. Thus a returned “Deferred” or “Error” action from a helper can differ from an escaping exception at this boundary. This is a source-reading finding, not an endorsement of those result semantics. Manual cloud retry's changed-binding check and membership persistence can still raise after remote acceptance. Fatal termination, such as `SystemExit` in L1, is not this ordinary result-mapping path. [Reserved result handling][reserved]

The unbound cleanup shortcut is conditional on no CloudRun and no binding, not merely an empty `provider_sessions` list. `finish_execution` and `reconcile` are separate operations; there is no universal finally-block release of the logical owner. An inherited execution is not finished by this outer finally branch. The code also retains an exact historical label-skip-string cleanup branch; `LabelManager` itself is side-effect-free, so that branch is not drawn as an ordinary label-lock protocol. [Outer finally][finally] [Label scope][labels]

## O2. Terminal PR-backed reclamation checks the whole observed owner

Entry: a due reclamation obligation, seeded by startup or a terminal-PR/owner trigger and serviced from the capacity-refill loop. This specialized retirement path covers ordinary Issue-owned, PR-backed local/Jules reservations. It is not a generic Codex/Claude, speculative, recurrent or never-published-owner release algorithm.

```mermaid
sequenceDiagram
    participant E as Startup or terminal-PR trigger
    participant D as Reclamation obligation store
    participant M as Capacity-refill consumer
    participant O as Retirement observer
    participant G as GitHub, Jules and local liveness
    participant S as Slot and retired-history stores
    E->>D: Schedule owner/incarnation inc for reevaluation
    M->>D: Read due obligations
    M->>S: Serialize owner and compare current incarnation
    break Obligation belongs to an old/absent incarnation
        M->>D: Clear only that stale obligation
    end
    M->>O: collect_retirement_observation
    O->>S: Capture incarnation, activity revision and memberships
    O->>G: Read complete PR attribution, sessions and execution evidence
    O-->>M: Observation with active/unknown evidence, or no scoped candidate
    alt No scoped observation
        M->>D: Clear obligation, not the active owner
    else Collection fails or work is active/unknown
        M->>D: Retain and reschedule check
    else Predicate permits retirement
        M->>S: retire_implementation_slot under owner/state locks
        S->>S: Recheck incarnation, activity revision and membership completeness
        alt Newer activity or incomplete evidence
            S-->>M: STALE_OBSERVATION or retained result
            M->>D: Reschedule without releasing capacity
        else Commit checks pass
            S->>S: Write retired history, then remove active reservation
            S-->>M: RELEASED
            M->>D: Clear obligation
            M->>M: Request capacity refill in this daemon run
        end
    end
```

PR candidates include retained implementation membership, native closing/Development associations, every attributed Jules PR output and restricted open-PR discovery. The observer distinguishes an unavailable listing from a confirmed empty set. Jules terminal state alone is not the predicate: publication and later activity evidence also matter. Any active PR/session/execution or continuing responsibility retains work; unknown evidence retains it rather than becoming success. [Observer][observer] [Predicate][predicate]

The commit checks `inc` and `rev` against the live record and covers stored memberships. History is written before active removal under locks; those are separate writes, not a demonstrated transaction across files and SQLite. The function can also record the generation tombstone when a routing argument is supplied, but the shown scheduler calls it without that optional argument. These diagrams therefore do not invent an extra routing write at this production call site. [Retirement commit][retire] [Due consumer][consumer]

Startup makes active Issue owners due, including ones missing from the open-PR enumeration because their PR closed while the daemon was offline. That only schedules observation. A `None` observation clears a reclamation obligation without proving the owner was released. A successful release requests refill; refill still performs fresh Issue admission and cannot bypass I1/I2. [Startup seeding and triggers][consumer]

## O3. GitHub operational deferral resumes an evaluation, not a send

Entry: an Issue hierarchy/readiness evaluation catches `GitHubRequestError` at a call site using `_defer_issue_evaluation`. Other exceptions and all provider outcomes are not implicitly this same mechanism.

```mermaid
sequenceDiagram
    participant E as Issue evaluator
    participant D as Pending-work store/scheduler
    participant H as IssueProcessingStageHandler
    participant G as GitHub
    participant A as Shared candidate admission
    E->>D: Retain Issue stage, content revision and unfinished effects
    E->>D: Wake scheduler
    E-->>E: DEFERRED with operational reason
    Note over D,H: Retained work is also available after daemon restart
    D->>H: dispatch or recover due obligation
    H->>G: Strict Issue dispatch snapshot
    alt Operational read failure
        H-->>D: StageOutcome(error)
    else Malformed/wrong-type target or changed content revision
        H-->>D: SUPERSEDED, no stale dispatch
    else Revision still matches
        H->>A: _process_single_candidate with fresh Issue data
        Note over H,A: Open/readiness, family, ownership and provider gates still apply
        A-->>H: Current processing result
        alt Evaluation is deferred again
            H-->>D: No completed effects claimed by this handler
        else Evaluation no longer deferred
            H-->>D: Complete retained evaluation effects
        end
    end
```

An effect marked complete here means the retained evaluation was handled, not that an Issue was implemented or merged. A changed revision supersedes this obligation; this handler does not itself manufacture a new-generation dispatch. Invalidation/startup discovery has its own entrypoint for changed entities. BLOCKED-review publication uses a different stage handler that tracks its diagnostic and readiness-withdrawal effects separately; it is not a provider task resend. [Deferral][deferral] [Resume handler][resume]

[reserved]: https://github.com/kitamura-tetsuo/auto-coder/blob/d704b54e59ea559cce17d5cfe1a0841ac3ef465d/src/auto_coder/automation_engine.py#L6290-L6415
[finally]: https://github.com/kitamura-tetsuo/auto-coder/blob/d704b54e59ea559cce17d5cfe1a0841ac3ef465d/src/auto_coder/automation_engine.py#L6150-L6190
[labels]: https://github.com/kitamura-tetsuo/auto-coder/blob/d704b54e59ea559cce17d5cfe1a0841ac3ef465d/src/auto_coder/label_manager.py#L340-L393
[observer]: https://github.com/kitamura-tetsuo/auto-coder/blob/d704b54e59ea559cce17d5cfe1a0841ac3ef465d/src/auto_coder/implementation_retirement_observer.py
[predicate]: https://github.com/kitamura-tetsuo/auto-coder/blob/d704b54e59ea559cce17d5cfe1a0841ac3ef465d/src/auto_coder/implementation_retirement.py#L144-L233
[retire]: https://github.com/kitamura-tetsuo/auto-coder/blob/d704b54e59ea559cce17d5cfe1a0841ac3ef465d/src/auto_coder/implementation_retirement.py
[consumer]: https://github.com/kitamura-tetsuo/auto-coder/blob/d704b54e59ea559cce17d5cfe1a0841ac3ef465d/src/auto_coder/implementation_reclamation_scheduler.py
[deferral]: https://github.com/kitamura-tetsuo/auto-coder/blob/d704b54e59ea559cce17d5cfe1a0841ac3ef465d/src/auto_coder/automation_engine.py#L6408-L6465
[resume]: https://github.com/kitamura-tetsuo/auto-coder/blob/d704b54e59ea559cce17d5cfe1a0841ac3ef465d/src/auto_coder/automation_engine.py#L496-L565
