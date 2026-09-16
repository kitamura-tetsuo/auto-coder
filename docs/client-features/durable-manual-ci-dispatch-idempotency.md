# Durable manual CI dispatch idempotency

  dispatch_claim_store:
    description: "Makes the manual CI `workflow_dispatch` boundary idempotent using a durable, restart-surviving claim store instead of the `@auto-coder` label or process-local state (Issue #1791)."
    implementation: |
      DispatchClaimStore, DispatchIdentity, DispatchOutcome, ClaimResult, get_dispatch_claim_store in src/auto_coder/dispatch_claim_store.py,
      WorkflowDispatchResult, trigger_workflow_dispatch, _classify_dispatch_exception in src/auto_coder/util/github_action.py,
      dispatch admission block in _handle_pr_merge in src/auto_coder/pr_processor.py
    behavior:
      - "A manual CI dispatch identity is the tuple of repository, pull-request number, authoritative PR head SHA observed for the dispatch decision, and the exact workflow identifier; a different head SHA or workflow identifier is a different identity and is never blocked by another identity's claim."
      - "Before invoking the external workflow_dispatch operation, Auto-Coder atomically acquires a durable SQLite-backed claim for the identity (DispatchClaimStore.try_acquire_claim, using a BEGIN IMMEDIATE transaction); concurrent workers racing for the same identity see exactly one acquisition succeed."
      - "Any failure, corruption, or uncertainty while connecting to, locking, reading, or writing the claim store fails closed: acquisition is denied and no workflow_dispatch call is made."
      - "Once a claim is durably recorded, controller restart, process crash, timeout, connection loss, or any outcome that does not prove GitHub rejected the request keeps that identity dispatch-suppressing; only a definitively rejected outcome (DispatchOutcome.REJECTED) makes the identity dispatchable again — accepted and indeterminate claims remain suppressing indefinitely."
      - "trigger_workflow_dispatch returns a WorkflowDispatchResult carrying one of three observable outcomes (ACCEPTED, REJECTED, INDETERMINATE) instead of a boolean; it is truthy only when ACCEPTED. Only a completed HTTP round trip with a 4xx client-error status is classified REJECTED; transport failures, timeouts, and 5xx responses are classified INDETERMINATE."
      - "Dispatch admission and duplicate suppression never read, add, remove, retain, or otherwise depend on the `@auto-coder` label; the label continues to serve its unrelated lifecycle-locking purpose."
      - "The in-process `_active_monitors` set remains only a same-process optimization to avoid redundant monitor threads; it is not the correctness oracle for dispatch admission, since it does not survive controller restart."
      - "Automatic processing of a newly created Issue is scheduled no earlier than 60 seconds after its GitHub creation time. Mutations coalesce without extending that deadline, and the post-deadline decision uses a fresh authoritative fetch; explicit operator processing remains immediate."
