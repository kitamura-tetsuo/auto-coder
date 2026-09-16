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
