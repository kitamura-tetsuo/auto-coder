# PR adversarial validation and correction

Source snapshot: **`e1da0e87eb7215d870cbaddd4737bcd9bd6a87e8`** (2026-10-15 JST). [Scope and notation](README.md).

## R1. Raw reviewer output is not the final approval authority

```mermaid
sequenceDiagram
    participant P as PR processor
    participant V as Pinned-head adversarial reviewer
    participant S as Saved review and accepted-findings state
    participant E as Review/publication effects
    participant M as Later merge gate
    P->>V: Validate captured PR head and applicable contract
    V-->>P: Raw verdict, findings and evidence
    P->>S: Retain review and derive effective application
    S-->>P: Effective outcome after accepted-findings projection
    P->>S: Persist effective application before dependent effects
    alt Effective non-pass or unresolved accepted findings
        P->>E: Publish/reconcile applicable review or correction work
        P-->>P: Do not treat raw PASS as merge permission
    else Effective approval is current
        P->>E: Apply review effects under applicable authority checks
        P->>M: Continue through remaining CI, thread and review-cycle gates
    end
```

Current-head/current-attempt evidence matters. A saved raw review is reusable only under its applicable identity rules; the highest applicable native attempt is not interchangeable with a legacy fallback. An `ADDRESSED` reply is not independent closure evidence. `_derive_effective_application` and saved non-pass replay preserve the distinction between a model claim and accepted-findings authority.

## R2. Explicit-local review repair can conflict with cloud ownership

```mermaid
sequenceDiagram
    participant P as PR processor
    participant O as PR markers and exact-PR cloud binding
    participant J as PR correction local-job acceptance
    participant C as Cloud continuation
    P->>O: Resolve current review-repair route for captured head H
    alt Explicit-local marker plus exact-PR cloud binding
        O-->>P: CONFLICT
        P-->>P: Do not silently choose either execution route
    else Explicit-local without conflicting binding
        O-->>P: LOCAL_REQUIRED
        P->>J: Revalidate head, route, repair claim and allowance
        J-->>P: Durable PR_REVIEW_CORRECTION job or refusal
    else Applicable cloud route
        O-->>P: CLOUD
        P->>C: Continue through authorized provider-specific path
    else No usable route
        O-->>P: UNAVAILABLE
        P-->>P: Defer/refuse rather than guess ownership
    end
```

The explicit markers include `<!-- auto-coder:local-llm -->` and `<!-- auto-coder:local -->`. A source Issue's cloud history alone is not an exact-PR binding. This routing classification is for review correction; it must not be copied as a description of every CI or conflict branch.

## R3. Local correction completion returns to independent validation

```mermaid
sequenceDiagram
    participant R as PR correction LocalJobRunner
    participant X as Correction executor
    participant S as Head, repair claim and reserved allowance
    participant F as Downstream effects
    participant P as Later PR processing
    R->>X: Execute captured correction input
    X->>S: Recheck same-repository head, route, threads and allowance
    X-->>R: Recorded result
    R->>F: Resume publication/notification progress
    F->>S: Retain awaiting-validation repair state when applicable
    F-->>R: Job may settle after its own downstream obligations
    P->>S: Read current repair and review evidence
    P->>P: Independently validate current head and finding closure
```

A settled correction job is not an approval. A published repair head can still have failed CI or unresolved accepted findings. Publication replay and validation must not cause inference to rerun merely because an earlier notification was incomplete.

Sources: [review orchestration and routing][pr] (`_derive_effective_application`, `_replay_saved_nonpass_review`, `_preflight_accepted_findings_merge_authority`); [correction executor/finalization][correction]; [durable job state][jobs].

[pr]: https://github.com/kitamura-tetsuo/auto-coder/blob/e1da0e87eb7215d870cbaddd4737bcd9bd6a87e8/src/auto_coder/pr_processor.py
[correction]: https://github.com/kitamura-tetsuo/auto-coder/blob/e1da0e87eb7215d870cbaddd4737bcd9bd6a87e8/src/auto_coder/pr_correction_job.py
[jobs]: https://github.com/kitamura-tetsuo/auto-coder/blob/e1da0e87eb7215d870cbaddd4737bcd9bd6a87e8/src/auto_coder/local_job_handoff.py

## R4. Extracted review coordinator preserves effect authority

`_handle_pr_merge` selects the green-CI review route and delegates its ordinary
and two-tier composition to `_coordinate_pr_review`. The typed coordinator
result reports what the invocation accomplished; it is not merge authority.
The caller-owned `PRReviewRuntime` retains admission and attempt-transition
contexts across the coordinator's final remote-head, CI, thread, current-attempt,
and two-tier checks and through the merge effect. Existing durable review-cycle,
effective-decision, correction-routing, publication, and merge helpers remain
the authoritative producers and consumers.
