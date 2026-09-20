# Issue provider dispatch: as-is sequences

Snapshot: **`d704b54e59ea559cce17d5cfe1a0841ac3ef465d`**. [Issue lifecycle and identities](issue-implementation.md). These sequences begin after I2 ownership admission. Remote model execution and PR publication are not synchronous consequences of a launcher returning.

## P1. Cloud selection is not provider acceptance

The `difficult` branch selects the high-score cloud helper before the ordinary `jules_mode` branch. The latter uses configured cloud providers; its name does not force Jules.

```mermaid
sequenceDiagram
    participant E as Reserved dispatcher
    participant H as Cloud route helper
    participant Q as Config and quota selector
    participant P as Provider-specific helper
    participant L as Local helper (L1)
    E->>H: High-score cloud or ordinary cloud route
    H->>Q: Read configured candidates and rank/filter by quota
    Q-->>H: Eligible ordered backend names
    break Configured pool has no eligible candidate
        H-->>E: CloudSubmissionNotStartedError
    end
    loop Candidate names until the route returns or raises
        H->>H: Resolve backend_type from configuration
        opt Supported Codex, Claude Routine or Jules type
            H->>P: Call provider-specific helper
            alt Helper returns an action list
                break Return from cloud route
                    P-->>H: Actions, possibly defer/error/tracking incomplete
                    H-->>E: Return without trying another candidate or fallback
                end
            else Usage-limit or definitely-not-started exception
                H->>H: Count rejection and try next candidate
            else Other exception
                alt Explicit retry authority or manual_retry
                    break Exit cloud route on exception
                        H-->>E: Propagate exception without fallback
                    end
                else Ordinary dispatch
                    H->>H: Log and try next candidate
                end
            end
        end
    end
    Note over H,L: Only loop exhaustion reaches the following fallback decision
    alt Nonempty pool and every candidate rejected before starting
        H-->>E: CloudSubmissionNotStartedError
    else Loop exhausts without that condition
        H->>L: Configured manager fallback or default local manager
        L-->>E: Local action list
    end
```

Ordinary cloud configuration prefers priority groups, then ordered backends, then a single configured backend, otherwise `jules`. High-score cloud uses its order or single backend. Unsupported backend types can fall through the loop. A provider helper returning a diagnostic list is still a return, not an exception that activates failover. Conversely, the ordinary generic-exception branch does not itself prove the preceding remote request was never accepted. These are existing routing distinctions, not recommended recovery policies. [Route helpers][routes]

## P2. Codex Cloud persists a suppressing claim before submission

Entry: `_process_issue_codex_cloud_mode` for ordinary dispatch. Its authoritative run key is repository + Issue + numeric attempt `a`. T2 adds another journal when explicit retry authority is supplied.

```mermaid
sequenceDiagram
    participant H as Codex Issue launcher
    participant R as CloudRunRepository
    participant C as CloudManager binding
    participant P as CodexCloudClient
    participant G as GitHub
    H->>R: Read run for I/a
    H->>C: Strictly read Issue binding
    break Unreadable or contradictory ownership
        H-->>H: Defer without submission
    end
    alt Existing run with task identity
        H->>C: Ensure matching binding
        H-->>H: Return reuse or tracking-incomplete action
    else Existing run lacks task identity or only legacy binding exists
        H-->>H: Defer, unresolved ownership needs attention
    else No suppressing ownership
        H->>H: Render initial implementation prompt and check drain
        break Graceful drain forbids new work
            H-->>H: Return deferred action without submission
        end
        H->>R: acquire_submission_claim with indeterminate outcome
        break Claim not acquired or cannot be persisted
            H-->>H: Return without submit_task
        end
        Note over H,R: C1: crash here suppresses later submission even before a send
        H->>P: submit_task(prompt, repository, base, title)
        P-->>H: Classified submission outcome
        H->>R: Persist outcome and returned task identity
        break Outcome persistence fails
            H-->>H: Report indeterminate, do not assume the task was absent
        end
        alt Definitely not submitted
            H->>R: Release definitely-not-submitted claim
            H-->>H: Raise not-started exception for routing
        else Indeterminate
            H-->>H: Retain claim and return attention-needed diagnostic
        else Accepted with task identity
            H->>C: Ensure exact provider/task/backend binding
            break Binding cannot be confirmed
                H-->>H: Return accepted-but-tracking-incomplete
            end
            H->>G: Comment with task identity and URL
            H-->>H: Return started-task action
        end
    end
```

A usage-limit exception has a separate path that marks the claim definitely-not-submitted, releases it and rethrows. An unexpected exception is not silently converted into an accepted receipt by this function. The no-task-ID claim remains suppressing on reentry. An accepted CloudRun with a missing CSV binding can repair tracking without calling `submit_task` again; a contradictory binding is refused. [Codex launcher][codex]

The Issue comment is after run and binding persistence and is not the deduplication authority. It is not caught locally at that final call, so its failure can reach the route helper even though the task is already recorded. Recovery of a known accepted task's initial PR is T4; that monitor is not a task-ID discovery mechanism for an indeterminate submission. [Accepted tail][codex-tail]

## P3. Singleton Jules and Claude Routine are send-then-bind paths

Entry: ordinary dispatch without explicit retry authority. Jules speculative width greater than one branches to P4 instead. The common shape below stops at acceptance/tracking; it does not equate these two clients' internal HTTP semantics.

```mermaid
sequenceDiagram
    participant H as Singleton Issue launcher
    participant P as Jules or Claude Routine client
    participant C as CloudManager
    participant G as GitHub
    participant E as Engine return boundary
    H->>H: Render issue.action with cloud context
    break Graceful drain forbids new work
        H-->>E: Return deferred action without starting session
    end
    alt Jules singleton
        H->>P: start_session(prompt, repository, base, title)
        P-->>H: session_id
    else Claude Routine
        H->>P: fire_routine(prompt, repository, base, title)
        P-->>H: session_id and optional URL
    end
    Note over H,C: C2: accepted session can precede durable Issue tracking
    H->>C: add_session(I, session_id, provider, backend)
    alt Tracking saved
        H->>H: Retain session association
    else Ordinary tracking failure
        H->>H: Append warning and continue
    end
    H->>G: Try to post session-start comment
    Note over H,G: Comment failure is caught and added as a warning
    H-->>E: Started/warning actions, provider continues asynchronously
    E->>C: Read binding for slot membership mirror (O1)
```

The ordinary singleton helpers do not use the Codex CloudRun claim or the speculative candidate-submission adapter. This does not erase I2's prior durable implementation start; it means that prior start and a provider-specific send receipt are different evidence. The Jules helper catches ordinary exceptions and returns error actions. Claude Routine rethrows usage-limit errors, but catches other exceptions in ordinary mode; retry mode instead propagates those errors. A returned action list is not proof of remote delivery. [Jules singleton][jules] [Claude Routine][claude]

With an explicit retry authority, both helpers first claim T2's request-scoped creation record and can repair an accepted session's binding without re-sending. Their retry replay paths guard against an older accepted request replacing a newer accepted request's current pointer. Do not infer this guard applies to every other provider path. `LabelManager.keep_label()` at these call sites is a no-op, not a GitHub label mutation. [Retry journal][retry] [Label scope][labels]

## P4. Speculative Jules captures a fixed candidate set

Entry: Jules route, configured width greater than one, and **no explicit retry authority**. The launcher refuses this path if independent PR adversarial validation is disabled or no backend can be constructed. This diagram expands submission only, not winner selection or cleanup.

```mermaid
sequenceDiagram
    participant H as Competition launcher
    participant L as JulesCompetitionLedger
    participant A as Candidate submission adapter
    participant D as Candidate request journal
    participant J as Jules
    participant S as Implementation slots
    H->>L: Load active generation or create fixed candidate set
    Note over H,L: Capture Issue oracle, source attempt, base and policy
    loop Persisted candidates while generation active and no winner
        H->>L: Recheck active generation and drain boundary
        H->>A: submit(repository, I, generation, candidate, owner, payload)
        A->>L: Read and validate generation, candidate and owner
        alt Initial authority is ineligible
            A-->>H: BLOCKED, launcher can continue to another candidate
        else Initial authority is eligible
            A->>D: Persist exact payload, branch and correlation marker
            alt Candidate already has a suppressing submission state
                A-->>H: Existing identity or BLOCKED, no new session
            else Candidate was NEVER_SUBMITTED
                A->>L: Claim candidate submission using namespace epoch
                opt Claim acquired
                    A->>L: Recheck candidate authority after claim
                end
                alt Claim lost or authority no longer allows send
                    A-->>H: BLOCKED without sending
                else Send remains admitted
                    A->>J: start_session with candidate branch/marker instructions
                    J-->>A: Identity or classified exception
                    A->>L: Record accepted, definitely-not-accepted or unknown result
                    A-->>H: Result with identity only when retained
                end
            end
        end
    end
    H->>S: Record returned candidate session identities on the same Issue owner
    H-->>H: Return competition summary
```

The loop submits candidates sequentially; remote sessions can run concurrently. It is not depicted as multiple independent Issue-slot acquisitions. Once a candidate is no longer NEVER_SUBMITTED, repeated `submit` calls do not automatically send another session; the separate reconciliation method uses captured correlation evidence. A returned unknown outcome is not a new candidate or a new generation. The prompt asks candidates to publish isolated PRs, but a prompt instruction is not proof that a remote artifact actually obeyed it. [Competition launcher][competition] [Candidate adapter][candidate]

[routes]: https://github.com/kitamura-tetsuo/auto-coder/blob/d704b54e59ea559cce17d5cfe1a0841ac3ef465d/src/auto_coder/issue_processor.py#L865-L1110
[codex]: https://github.com/kitamura-tetsuo/auto-coder/blob/d704b54e59ea559cce17d5cfe1a0841ac3ef465d/src/auto_coder/issue_processor.py#L631-L810
[codex-tail]: https://github.com/kitamura-tetsuo/auto-coder/blob/d704b54e59ea559cce17d5cfe1a0841ac3ef465d/src/auto_coder/issue_processor.py#L810-L863
[jules]: https://github.com/kitamura-tetsuo/auto-coder/blob/d704b54e59ea559cce17d5cfe1a0841ac3ef465d/src/auto_coder/issue_processor.py#L213-L385
[claude]: https://github.com/kitamura-tetsuo/auto-coder/blob/d704b54e59ea559cce17d5cfe1a0841ac3ef465d/src/auto_coder/issue_processor.py#L445-L630
[competition]: https://github.com/kitamura-tetsuo/auto-coder/blob/d704b54e59ea559cce17d5cfe1a0841ac3ef465d/src/auto_coder/issue_processor.py#L386-L444
[candidate]: https://github.com/kitamura-tetsuo/auto-coder/blob/d704b54e59ea559cce17d5cfe1a0841ac3ef465d/src/auto_coder/jules_candidate_submission.py#L99-L249
[retry]: https://github.com/kitamura-tetsuo/auto-coder/blob/d704b54e59ea559cce17d5cfe1a0841ac3ef465d/src/auto_coder/retry_dispatch.py
[labels]: https://github.com/kitamura-tetsuo/auto-coder/blob/d704b54e59ea559cce17d5cfe1a0841ac3ef465d/src/auto_coder/label_manager.py#L340-L393
