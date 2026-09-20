# PR intake, CI and repair: as-is sequences

Snapshot: **`95ff379d2bd69e54a53f14c208eedaca5a58e3df`**. [Scope and notation](README.md).

## C1. PR entry and the shared CI routing boundary

Entry: `process_pull_request`, after the engine's shared admission/ownership path. Some safety and cleanup checks also occur in the reserved engine boundary and again in `_handle_pr_merge`; those are repeated checks, not distinct lifecycle transitions.

```mermaid
sequenceDiagram
    participant E as Engine PR dispatch
    participant P as PR processor
    participant J as Speculative Jules lifecycle
    participant G as GitHub
    participant D as CI watches and review recovery stores
    participant R as Ordinary review (R1)
    E->>P: process_pull_request(current PR data)
    P->>J: Evaluate competition artifact authority when configured
    break Loser or uncertain artifact
        P-->>E: DEFERRED before ordinary cleanup, repair or merge
    end
    P->>G: Refresh/reject unsafe Codex branch, check empty/stale PR closure
    break Recovery closed PR or metadata is unavailable
        P-->>E: Return closure or deferral result
    end
    P->>P: Check Jules wait, attempt provider linkage, check wait again
    P->>G: Initial CI observation
    P->>P: Unified action route reaches _handle_pr_merge
    P->>G: Repeat unsafe-branch authority check
    P->>D: Ensure current-head CI watch, retire old-head watches
    P->>D: Retry stale-thread rollbacks when thread gate enabled
    break Watch unavailable or rollback integrity unresolved
        P-->>E: Return without merge
    end
    P->>P: Apply authorized review-adjudication effects
    P->>G: Check running CI and mergeability
    opt Explicitly unmergeable and remediation enabled
        P->>P: Check repair allowance, request remediation or block
        break Remediation path terminates this invocation
            P-->>E: Return before ordinary review
        end
    end
    alt CI still in progress
        P->>D: Schedule current-head CI-watch recheck in 30 seconds
        P-->>E: DEFERRED
    else Detailed CI read has an error
        P-->>E: FAILED, no review or merge
    else No CI check IDs for current head
        P->>P: Manual dispatch sequence C2
    else CI succeeds
        P->>R: Continue only when AUTO_MERGE and labels permit
    else Non-success reaches failure route
        P->>P: CI repair routing C3
    end
```

The unmergeable branch returns after remediation only when that path is enabled; disabled remediation does not itself terminate the rest of the function. Repair exhaustion can stop it first. Review-adjudication application exceptions are caught and logged in this function, then processing continues with no adjudication actions; the diagram does not imply every auxiliary failure closes every gate.

Despite old call-site comments, `LabelManager` does not read, add or remove the historical processing label. Provider linkage is best effort. The post-merge loop that searches for other PRs with a Jules session ID contains no closing mutation in this snapshot; it is not depicted as automatic sibling-PR closure.

Sources: [PR entry][entry], [shared CI boundary][ci-boundary], [label scope implementation][labels], [post-merge handling][final-route].

## C2. No checks: claim, watch, dispatch, record

Precondition: `_handle_pr_merge` obtained a detailed status with no check IDs. The workflow identifier in this path is literally `ci.yml`; the code sends the PR's `head.ref` as the dispatch ref. The claim records head SHA `H`, but this diagram does not assert that a moving branch ref is an immutable remote execution target.

```mermaid
sequenceDiagram
    participant P as PR processor
    participant C as DispatchClaimStore (SQLite)
    participant W as Durable CI-watch store
    participant T as trigger_workflow_dispatch
    participant G as GitHub Actions
    P->>C: try_acquire_claim(repository, N, H, ci.yml)
    alt Claim absent or prior state REJECTED
        C->>C: BEGIN IMMEDIATE, write PENDING with holder token, COMMIT
        C-->>P: acquired(holder_id)
    else Suppressing state or store unavailable
        C-->>P: acquisition refused
        break Not acquired
            P-->>P: Return without workflow_dispatch
        end
    end
    Note over P,C: C1: crash after committed claim, before dispatch
    P->>W: ensure_ci_watch(repository, N, H, ci.yml)
    break Watch cannot be persisted
        P-->>P: Return, claim remains suppressing
    end
    P->>T: Dispatch ci.yml using head.ref
    T->>G: workflow_dispatch
    Note over T,G: C2: remote acceptance and lost response are distinguishable from rejection only when observed
    T-->>P: Classified dispatch outcome
    Note over P,C: C3: crash before recording the returned outcome
    P->>C: record_outcome(key, outcome, holder_id)
    alt Durable outcome write succeeds
        C-->>P: Recorded
    else Write fails
        C-->>P: False, prior suppressing claim remains
    end
    P-->>P: Return handoff/failure diagnostic, later CI observation is separate
```

| Interruption/evidence at restart | What this code actually admits |
| --- | --- |
| No committed claim | A later call can acquire, provided the store is readable/writable. |
| C1: PENDING committed, dispatch never reached | The same key is still suppressed. The store has no automatic timeout that proves the request was never sent. |
| C2 or C3: send may have succeeded, outcome not durably recorded | PENDING/INDETERMINATE suppress redispatch of that key. Missing workflow evidence is not implemented here as permission to release it. |
| REJECTED durably recorded | A later acquisition changes it back to PENDING with a new holder token. |
| A different head SHA | A different dispatch key; an old-head claim does not occupy that key. |

The claim and CI watch are separate stores, not one atomic transaction. A persistence failure between them can therefore leave a suppressing claim without this dispatch's watch registration. The initial general current-head watch in C1 is a separate earlier operation. No exactly-once external-execution guarantee is inferred.

Sources: [caller ordering][dispatch-caller], [complete claim-store implementation][claim-store].

## C3. CI repair dispatch is provider- and checkout-dependent

Precondition: the non-success CI branch obtained detailed failed checks and has not been stopped by the repair-allowance guard. Green CI followed by a deferred/failed merge returns in its own route and does **not** enter this sequence.

```mermaid
sequenceDiagram
    participant P as PR processor
    participant A as Repair allowance and CI authority
    participant X as Originating cloud helper
    participant W as Local workspace
    participant F as Local fix/test helpers
    P->>A: Check open-blocker repair exhaustion
    break Repair allowance exhausted
        P-->>P: Publish deduplicated exhaustion diagnostic, stop
    end
    alt Codex-created PR
        P->>X: _send_codex_cloud_error_feedback
        X-->>P: delivered / retryable / actions
        P-->>P: Return, local repair remains disabled even on PR checkout
    else Non-Codex PR
        P->>W: Read current branch
        alt Jules PR and not already on its branch
            P->>P: Try stale-Jules closure
            opt PR was not closed
                P->>X: _send_jules_error_feedback
                X-->>P: Actions
            end
            P-->>P: Return without local repair
        else Local repair candidate
            P->>P: Exclude dependency bots and unauthorized local origins
            P->>P: Check automatic-test-fix flag and checkout override
            P->>A: current_ci_failure_authority(repository, N, H)
            break Current failure authority refused
                P-->>P: Defer before branch preparation or edits
            end
            P->>W: Prepare branch if necessary, enter BranchManager
            alt Skip base update policy
                P->>F: Summarize failed checks and fix with testing
            else Base update enabled
                P->>F: _update_with_base_branch
                F-->>P: Degrading / pushed / up-to-date / other result
                opt Up-to-date and test fixing enabled
                    P->>F: Repair with CI logs and testing
                end
            end
            W-->>P: Exit branch context
        end
    end
```

A Jules PR already checked out locally may reach the local candidate branch; Codex classification takes precedence over that override. In the base-update path, a degrading-merge flag closes the PR, a pushed update returns to await CI, and an up-to-date result can proceed to tests. No specific failed checks means the respective repair call is skipped.

The diagram records a **call** to the Jules feedback helper, not a confirmed external receipt: this caller appends a handoff diagnostic after that helper returns. Likewise, entering a provider helper does not prove that quota, ownership, deduplication or transport checks inside it permitted a send. Those provider-specific internals are not flattened into an unconditional remote message here.

Sources: [green-CI false return and Codex precedence][final-route], [Jules/local eligibility and mutation boundary][repair].

[entry]: https://github.com/kitamura-tetsuo/auto-coder/blob/95ff379d2bd69e54a53f14c208eedaca5a58e3df/src/auto_coder/pr_processor.py#L1099-L1350
[ci-boundary]: https://github.com/kitamura-tetsuo/auto-coder/blob/95ff379d2bd69e54a53f14c208eedaca5a58e3df/src/auto_coder/pr_processor.py#L3137-L3270
[labels]: https://github.com/kitamura-tetsuo/auto-coder/blob/95ff379d2bd69e54a53f14c208eedaca5a58e3df/src/auto_coder/label_manager.py#L340-L393
[dispatch-caller]: https://github.com/kitamura-tetsuo/auto-coder/blob/95ff379d2bd69e54a53f14c208eedaca5a58e3df/src/auto_coder/pr_processor.py#L3271-L3380
[claim-store]: https://github.com/kitamura-tetsuo/auto-coder/blob/95ff379d2bd69e54a53f14c208eedaca5a58e3df/src/auto_coder/dispatch_claim_store.py#L1-L243
[final-route]: https://github.com/kitamura-tetsuo/auto-coder/blob/95ff379d2bd69e54a53f14c208eedaca5a58e3df/src/auto_coder/pr_processor.py#L4380-L4470
[repair]: https://github.com/kitamura-tetsuo/auto-coder/blob/95ff379d2bd69e54a53f14c208eedaca5a58e3df/src/auto_coder/pr_processor.py#L4470-L4635
