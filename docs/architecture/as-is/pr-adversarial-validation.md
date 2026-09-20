# Ordinary adversarial validation: as-is sequences

Snapshot: **`95ff379d2bd69e54a53f14c208eedaca5a58e3df`**. [Scope and notation](README.md).

## R1. Review-thread admission, eligibility and same-head reuse

Continuation from green CI in C1, with automatic merge enabled and no `disable-auto-merge` label. Ordinary validation, optional strong review, and final merge authorization are different boundaries.

```mermaid
sequenceDiagram
    participant P as PR processor
    participant G as GitHub reviews and Issue oracles
    participant F as Cloud review-repair helper
    participant A as Review audit
    participant V as Fresh validation (R2)
    participant N as Two-tier/final merge gates
    P->>G: Classify current review threads when gate enabled
    opt Unresolved blocking threads
        P->>F: Delegate eligible repair threads, unless allowance exhausted
        Note over P,F: Provenance-clarification threads wait without requesting code repair
        alt Normal admission still blocked
            break No revalidation exception applies
                P-->>P: Return, no merge
            end
        else Explicit force permits validation
            Note over P,G: Force does not resolve threads or grant merge permission
        end
    end
    opt Ordinary validator enabled and PR not exempt
        P->>G: Resolve linked-Issue specification oracle
    end
    alt Validator disabled, dependency-bot exemption or confirmed no oracle
        P->>N: Skip ordinary validation, later gates still apply
    else Oracle lookup fails
        P-->>P: Stop, unavailable is not confirmed absence
    else Ordinary validation is applicable
        P->>G: Read count, saved status and new provenance/retry evidence
        alt Review-count shortcut applies
            P->>P: Enforce explicit gap/repair-exhaustion blockers
            P->>N: Skip fresh validation if those guards allow
        else Validation path remains enabled
            P->>G: Check Codex GitHub review completion, recheck threads as needed
            break Codex review incomplete or required lookup fails
                P-->>P: Return without starting ordinary validator
            end
            P->>G: Read authoritative published status for exact H
            alt Reusable same-head result, no force/new evidence/due retry
                P->>A: record_reused(result, H, source review when known)
                alt Saved PASS
                    P->>N: Continue without ordinary backend invocation
                else Saved non-PASS
                    opt NEEDS_FIX or NEEDS_TESTS and allowance permits
                        P->>F: Replay applicable published report to originating task helper
                    end
                    P-->>P: Return without merge
                end
            else New attempt required
                P->>V: Clear saved status as authority, start R2
            end
        end
    end
```

Authoritative native reviews are selected using the dedicated reviewer App identity, exact-head marker and highest allocated attempt sequence, not response arrival order. The published-report helper also has a legacy-comment fallback. Reuse does not invent a missing historical audit `review_id`.

The review-count shortcut is important as-is behavior: under its conditions, the code sets `should_run_validation=False` and proceeds toward later gates. It explicitly stops unresolved specification gaps, `NEEDS_TESTS`, and exhausted repair allowance in that branch. This diagram therefore does **not** assert that every route reaching the two-tier `ordinary_pass` call executed a fresh ordinary PASS. New provenance evidence, older-head findings, explicit force and a due EXHAUSTED retry alter admission as shown in the source.

With the thread gate disabled, validation can still inspect claimed-addressed threads, but a lookup error in that optional read does not itself block the PR. Review results and final unresolved-thread gates remain separate; a scheduling predicate reporting “not adversarially blocked” is not merge permission.

Sources: [thread/oracle admission][admission], [count, Codex wait and reuse][reuse], [native-review ordering][native], [published-report fallback][report-fallback].

## R2. Execute at an exact head, then decide whether its effects are applicable

Precondition: R1 chose a fresh ordinary attempt. The scheduler lease is optional and process-local; the attempt repository provides persistent attempt identity/sequence. The diagram expands acceptance of a result with a reviewer-session checkpoint, and shows the condition explicitly.

```mermaid
sequenceDiagram
    participant P as PR processor
    participant L as AdversarialValidationScheduler
    participant D as Attempt repository
    participant A as Executed-review audit
    participant W as Detached worktree and validator
    participant G as GitHub
    participant S as ReviewerSessionRegistry
    opt Scheduler supplied by caller
        P->>L: admit(repository, N)
        break Duplicate local validation trigger
            P-->>P: Return without new attempt
        end
    end
    P->>D: start(N, H) -> attempt V1, allocated sequence
    P->>A: begin_executed_review -> review_id
    P->>W: Fetch pull head, create detached worktree at H, verify HEAD
    P->>W: run_adversarial_validation(execution_cwd, CI refresh callback)
    W-->>P: Result or exception converted to ERROR
    W->>W: Remove worktree and reset command execution context
    P->>A: finish_executed_review, retain semantic report
    Note over P,D: Enter serialized acceptance transition
    P->>D: Read latest sequence for N and H
    break V2 started after V1
        P->>A: Record V1 as superseded, retain history
        P-->>P: Return before publication/closure effects
    end
    opt Result carries reviewer_session_checkpoint
        P->>G: Strict current head read
        break H changed or current head unavailable
            P->>A: Record superseded or failed acceptance
            P-->>P: Return without accepting checkpoint
        end
        opt Checkpoint contains evidence-validation snapshot
            P->>P: Verify snapshot is still applicable
        end
        P->>S: Save accepted checkpoint
        Note over P,S: Save failure converts result to ERROR and clears thread dispositions
    end
    P->>P: Continue to thread effects and publication (R3)
    Note over P,L: Finally finish this attempt, then release its admission lease
```

A failed snapshot applicability check returns before saving. The strict acceptance-time head read shown above is **conditional on a checkpoint**; it is not silently generalized to every returned result. The final merge head check is another boundary (M1). An older-head result may remain historical evidence without authorizing a newer head.

Starting a forced new attempt clears the saved published status for this invocation: a new ERROR does not fall back to a saved PASS. The semantic audit is retained before downstream publication effects, so an audit record does not prove that a GitHub review was published. The attempt is finalized in `finally`, including error paths, before closing the optional scheduler lease.

Sources: [pinned worktree setup][worktree], [attempt execution and checkpoint acceptance][execution], [attempt finalization][finalization].

## R3. Thread dispositions, publication, reconciliation and rollback

Precondition: R2 survived applicable attempt/checkpoint guards. Thread dispositions may be applied independently of the PR-level verdict; publication success is a separate observable effect.

```mermaid
sequenceDiagram
    participant P as PR processor
    participant T as Thread-validation helpers and blocker ledger
    participant G as GitHub reviewer App / threads
    participant A as Review audit and attempt repository
    participant F as Originating-task feedback helper
    opt Claimed threads have independent dispositions
        P->>T: resolve_addressed_review_threads(H, base, attempt)
        T->>G: Apply authorized thread-resolution effects
        T-->>P: Resolved IDs or stale-resolution error
        break Stale resolution could not be rolled back
            P-->>P: Return without merge
        end
    end
    P->>P: Enforce unresolved-provenance gate, format report
    opt Codex remediation snapshot was captured
        P->>P: Persist report-to-remediation snapshot before publication
    end
    P->>G: publish_adversarial_review(N, H, exact result)
    alt Publication reports success
        P->>A: Record confirmed publication effect
    else Publication is not confirmed
        P->>A: Record pending effect, not assumed failure/absence
        P->>G: Reconcile expected durable report
        alt Matching durable result is confirmed
            P->>A: Record confirmed reconciliation
        else No confirmation
            opt Threads were resolved in this invocation
                P->>T: Reopen after publication failure
                T->>G: Attempt rollback, retain unresolved rollback work
            end
            P->>A: Record failed/unknown effect, retain semantic report
            break Publication remains unconfirmed
                P-->>P: Return without merge
            end
        end
    end
    P->>A: mark_published, check newer published sequence
    alt NEEDS_FIX or NEEDS_TESTS
        P->>F: Request applicable cloud/author correction unless draining
        P-->>P: Return without local automatic adversarial repair
    else Non-passing or specification-gap verdict
        P-->>P: Return blocked, failed or quota-deferred
    else PASS permitting automatic merge
        P->>P: Continue to optional two-tier gate, then M1
    end
```

A newer published sequence returns as superseded before verdict-driven actions. A failed Codex remediation-snapshot write stops before publication. Generic thread-disposition exceptions are logged in the caller; the explicit stale-resolution rollback failure has the stronger stop shown above. On subsequent processing, pending stale rollbacks are retried before CI (C1).

| Ordinary result | Observed routing in this caller |
| --- | --- |
| `PASS` with automatic merge allowed | Continue to two-tier/final gates, not directly to merge. |
| `PASS_WITH_SPECIFICATION_GAPS` / pass that disallows auto-merge | Stop for unresolved specification policy gaps. |
| `NEEDS_FIX` | Ask originating cloud task/author for corrections; no local automatic adversarial code fix. |
| `NEEDS_TESTS` | Ask for focused regression protection, not production changes solely for the test gap. |
| `ERROR` | Failure diagnostic; no merge; can be reused as non-PASS on same-head admission. |
| `BLOCKED`, `INCONCLUSIVE` | Stop; no EXHAUSTED-style automatic same-head retry bypass here. |
| `EXHAUSTED` | Defer with published retry-not-before evidence; once due, R1 can admit same-head revalidation. This alone does not prove every caller schedules that wake. |

The table describes the ordinary verdict branch. It does not erase the preceding count-limit shortcut or any independent final gate.

Sources: [acceptance, resolution, publication and reconciliation][execution], [post-publication verdict routing][verdicts], [same-head EXHAUSTED handling][reuse].

[admission]: https://github.com/kitamura-tetsuo/auto-coder/blob/95ff379d2bd69e54a53f14c208eedaca5a58e3df/src/auto_coder/pr_processor.py#L3380-L3555
[reuse]: https://github.com/kitamura-tetsuo/auto-coder/blob/95ff379d2bd69e54a53f14c208eedaca5a58e3df/src/auto_coder/pr_processor.py#L3550-L3785
[native]: https://github.com/kitamura-tetsuo/auto-coder/blob/95ff379d2bd69e54a53f14c208eedaca5a58e3df/src/auto_coder/pr_processor.py#L2840-L2880
[report-fallback]: https://github.com/kitamura-tetsuo/auto-coder/blob/95ff379d2bd69e54a53f14c208eedaca5a58e3df/src/auto_coder/pr_processor.py#L3047-L3063
[worktree]: https://github.com/kitamura-tetsuo/auto-coder/blob/95ff379d2bd69e54a53f14c208eedaca5a58e3df/src/auto_coder/pr_processor.py#L3064-L3120
[execution]: https://github.com/kitamura-tetsuo/auto-coder/blob/95ff379d2bd69e54a53f14c208eedaca5a58e3df/src/auto_coder/pr_processor.py#L3770-L4021
[verdicts]: https://github.com/kitamura-tetsuo/auto-coder/blob/95ff379d2bd69e54a53f14c208eedaca5a58e3df/src/auto_coder/pr_processor.py#L4010-L4120
[finalization]: https://github.com/kitamura-tetsuo/auto-coder/blob/95ff379d2bd69e54a53f14c208eedaca5a58e3df/src/auto_coder/pr_processor.py#L4609-L4635
