# Codex Cloud initial PR recovery

The continuously running service recovers repository-scoped accepted Codex
Cloud runs and polls each coherent current Issue attempt independently.  A
fresh completed current-turn observation with complete negative GitHub PR
evidence starts a durable 120-second grace period.  If a fresh pre-send check
still finds no matching PR, the service sends the existing task one exact
`Create PR` message with QA mode disabled.  A crash-safe claim permits at most
one such automatic POST per task, including rejected or indeterminate sends.

Only a verified matching GitHub PR counts as publication.  Open PRs discovered
by polling are handed to the ordinary durable PR-processing queue without a
webhook; publication remains monotonic, and accepted/indeterminate reminders
continue observation without minting a replacement task or reminder budget.

Initial-publication polling ends durably once an open PR has been accepted by
the ordinary PR queue. A verified previously published closed/merged PR also
completes recovery without queueing it. Both paths retain the PR number and mark
the handoff responsibility complete for retirement accounting. Later unavailable
provider or GitHub observations cannot revive this completed recovery, including
after a daemon restart. Failed queue admission or completion persistence remains
retryable; publication alone never suppresses an unfinished open-PR handoff.
Missing or unrecognized GitHub PR lifecycle state remains unknown and cannot
establish closed/merged completion.

This monitor has no execution-trace scope of its own. Completed recovery emits
no further waiting logs or synthetic PR-processing events; ordinary queued PR
processing and capacity reclamation retain their existing dashboard traces.
