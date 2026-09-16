# Claude Routine PR ownership recovery

Claude Routine PR URLs (`https://claude.ai/code/session_...`) are matched against
provider sessions already recorded in the repository's implementation-slot store.
A matching PR reuses the existing logical owner even at full capacity, and its
PR membership survives restart without requiring the URL to remain in the body.
Startup discovery uses the same resolution before reconciliation. Unknown
sessions and ordinary Issue mentions do not create ownership links. PR detail
traces expose `pr.implementation-admission` with the resolved owner and the
admission or deferral outcome. Regression coverage includes
`test_claude_pr_reuses_only_recorded_session_at_full_capacity`,
`test_startup_discovers_claude_pr_from_recorded_session`, and
`test_claude_pr_slot_admission_reaches_detail_view`.
