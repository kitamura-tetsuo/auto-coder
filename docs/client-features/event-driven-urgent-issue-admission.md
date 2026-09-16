# Event-driven urgent Issue admission

  urgent_issue_admission:
    description: "Reconsiders a newly urgent Issue immediately through the shared durable implementation admission boundary (Issue #1767)."
    implementation: |
      urgent_admission in DurableInvalidationQueue and Candidate,
      process_github_payload in src/auto_coder/webhook_server.py,
      _worker_loop and _process_single_candidate_unified in src/auto_coder/automation_engine.py
    behavior:
      - "Only an exact `issues.labeled` transition for `urgent` records the durable urgent-admission obligation; the webhook snapshot is a wakeup source and workers perform cache-bypassing authoritative reads before evaluation."
      - "Urgent processing uses the ordinary specification, hierarchy, relationship, authorization, ownership, routing, quota, duplicate-execution, and dispatch path. Urgency changes only whether the single emergency capacity lane may be atomically selected by the shared implementation-slot repository."
      - "An authoritative pre-admission read must still show the Issue open and urgent. Removing `urgent` before admission prevents the stale event from authorizing emergency capacity."
      - "When capacity alone defers an urgent transition, its durable obligation returns to the dirty set and retries without another webhook; operational failures receive the same retry treatment. Non-capacity rejection completes the evaluated obligation."
      - "Coalescing and delivery deduplication key obligations by repository and stable Issue number, while durable implementation ownership prevents duplicate or concurrent deliveries from producing another logical owner or execution."
