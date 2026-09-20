# Speculative Jules lifecycle quarantine

Pull requests durably associated with a Jules competition are classified before
ordinary PR effects. Selected results may continue through normal processing;
active unselected, uncertain, and conflicting artifacts are deferred, while
verified retired artifacts are fenced and recorded for durable cleanup. A force
target does not bypass this authority check.

Cleanup is an independent replayable operation. It rechecks authoritative
candidate membership immediately before closing a PR, confirms the resulting
GitHub state before acknowledging the obligation, and leaves denied, ambiguous,
or unconfirmed requests pending across restart. Already closed or merged PRs need
no further close. Cleanup never mutates the source Issue, advances attempts,
deletes branches, or claims that a remote Jules session was cancelled.
