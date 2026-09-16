# Failed Cloud submission capacity recovery

Ordinary and difficult-Issue Cloud dispatch tries the remaining configured Cloud
candidates after a confirmed pre-submission rejection. If every candidate rejects
submission (including unavailable quota), dispatch returns a deferred outcome
without retrying the exhausted Cloud backends through the local fallback.
After its execution ends, the Issue releases its slot atomically only if no other
execution, provider session, or implementation PR remains. This also applies to
an existing empty reservation retried by the current dispatch. Accepted tasks,
indeterminate submissions, and unreadable submission journals retain ownership.
The dashboard records `issue.cloud-submission-slot-release` with `slot_released`
to distinguish recovered capacity from ownership retained for other work.
