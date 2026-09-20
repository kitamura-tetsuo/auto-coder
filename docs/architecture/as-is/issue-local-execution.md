# Local Issue execution: as-is sequences

Snapshot: **`d704b54e59ea559cce17d5cfe1a0841ac3ef465d`**. [Issue lifecycle and identities](issue-implementation.md).

## L1. Working branch, editing invocation and post-processing

Entry: `_take_issue_actions` followed by `_apply_issue_actions_directly`, after engine admission. The diagram expands a regular Issue without `head_branch`, not the helper's PR-shaped input variant. Explicit retry wraps this helper with the T2 creation journal before these effects.

```mermaid
sequenceDiagram
    participant E as Admitted Issue dispatcher
    participant H as Local Issue helpers
    participant G as GitHub context
    participant W as Git workspace and BranchManager
    participant B as Selected local backend
    participant C as Commit/push helper
    participant P as PR publication (L2)
    E->>H: _take_issue_actions(I, optional manager, slots)
    H->>G: Read parent and sub-Issue context
    H->>H: Read numeric attempt a and derive work branch
    H->>W: Check branch existence and enter BranchManager
    Note over H,W: issue-I for a=0, otherwise issue-I_attempt-a
    H->>H: Render issue.action with labels and context
    break Graceful drain forbids new work
        H-->>E: Deferred action, no editing invocation
    end
    H->>B: _run_llm_cli(prompt) under invocation admission
    B-->>H: Response and workspace edits
    H->>H: Confirm local invocation settled
    Note over H,W: C1: edits may exist before commit, push or PR creation
    alt Nonempty response
        H->>H: Append response-derived action strings
        Note over H,G: The close/comment API calls beside these strings are commented out
        H->>C: commit_and_push_changes
        C->>W: Inspect status, stage, commit and push when applicable
        C-->>H: Success, no changes or returned failure string
        Note over H,P: Returned failure/no-changes strings do not gate the next call
        H->>P: _create_pr_for_issue using work branch and configured base
        P-->>H: Publication action string
    else Empty response
        H->>H: Append unclear-response action
        Note over H,P: No controller commit/push or PR call on this branch
    end
    H->>W: Exit BranchManager
    H-->>E: Actions, engine cleanup continues in O1
```

The local `issue.action` call supplies the Issue body truncated to 10,000 characters, parent context and sub-Issue summary where present, label-selected prompt inputs, commit history and configured main branch. These are the inputs actually assembled here, not a promise that every Requirement reaches or is followed by the model. Existing work branches are reused; absent branches are created through `BranchManager`. The helper also contains a sub-Issue-container branch that resets/cleans/checks out/pulls before creation, but I1 normally routes submitted containers instead of assuming that helper branch is the ordinary production path. [Local wrapper][local] [Branch and prompt assembly][response]

There is **no separate controller test-success gate in this illustrated helper between the editing response and commit/PR creation**. The backend may run tests and Git hooks may run checks; those are not evidence that this call site inspected a passing test result. PR CI and merge gates occur later in the PR series. Similarly, nonempty output containing `closed`, `duplicate` or `invalid` produces a “Closed issue” action string, but the adjacent `close_issue` call is commented out; the other branch's “Added analysis comment” also has no corresponding API call. [Response handling][response]

`commit_and_push_changes` returns immediately on empty porcelain output. Staging and push failures can return strings; an unrecovered commit failure instead calls `save_commit_failure_history`, which attempts to write a diagnostic and raises `SystemExit(1)`. That termination is not the returned-failure path that proceeds to L2. Git recovery helpers may invoke additional LLM assistance; the sequence is not a claim of exactly one model call for the entire lifecycle. The local helper rethrows `AutoCoderRetryableBackendError` but logs other ordinary exceptions and returns accumulated actions. [Commit/push and exit][commit]

## L2. PR description, create-or-find and association

Entry: `_create_pr_for_issue` after the L1 call, including after a returned commit/push failure or no-changes result. No PR creation success is assumed at entry.

```mermaid
sequenceDiagram
    participant H as PR creation helper
    participant B as No-edit message backend
    participant G as GitHub
    participant S as Implementation slots
    H->>B: Generate PR title/body from bounded context and commit log
    B-->>H: JSON text, unavailable output or exception
    H->>H: Use parsed fields or fallback title/body
    H->>H: Ensure Closes reference and local-LLM marker
    H->>G: Validate Issue references
    break Reference validation fails
        H-->>H: Return validation-failed action, no create call
    end
    H->>G: Find PR by work-branch head
    alt Existing PR found
        H->>S: Record implementation PR membership if slots supplied
        H-->>H: Return existing-PR action
    else No existing PR found
        H->>G: Create PR with work head and configured base
        break Create call raises
            H-->>H: Return failure action, remote outcome may be unknown
        end
        G-->>H: PR response
        Note over H,S: C2: GitHub may contain the PR before local membership is written
        H->>S: Record implementation PR membership if number and slots supplied
        break Required membership write fails
            H-->>H: Return failure action without undoing GitHub PR
        end
        opt PR number returned
            H->>H: Wait two seconds
            opt Label copying enabled and urgent resolves
                H->>G: Copy urgent label and append urgent body note
            end
            H->>G: Read closing-Issue associations
            Note over H,G: Missing expected association is logged, not a create rollback
        end
        H-->>H: Return created-PR action
    end
```

The message backend is a separate no-edit call. Failure to obtain usable title/body falls back locally rather than proving implementation failed. Label copying in this path is limited to `urgent`; it does not copy every resolved semantic label. The existing-PR early return records membership but does not run the new-PR label/link-verification tail. [PR helper][publication]

A transport exception or failed membership write returns a failure action but does not undo an already-created PR. The find-then-create lookup is not presented as a durable PR-creation claim or an exactly-once guarantee. C2 records the ordering only: later discovery can associate PRs, but this diagram does not invent immediate recovery for an unknown create result. A logged closing-association mismatch can coexist with a returned “Successfully created PR” message. [Publication and exceptions][publication]

[local]: https://github.com/kitamura-tetsuo/auto-coder/blob/d704b54e59ea559cce17d5cfe1a0841ac3ef465d/src/auto_coder/issue_processor.py#L105-L210
[response]: https://github.com/kitamura-tetsuo/auto-coder/blob/d704b54e59ea559cce17d5cfe1a0841ac3ef465d/src/auto_coder/issue_processor.py#L1607-L1870
[commit]: https://github.com/kitamura-tetsuo/auto-coder/blob/d704b54e59ea559cce17d5cfe1a0841ac3ef465d/src/auto_coder/git_commit.py
[publication]: https://github.com/kitamura-tetsuo/auto-coder/blob/d704b54e59ea559cce17d5cfe1a0841ac3ef465d/src/auto_coder/issue_processor.py#L1413-L1606
