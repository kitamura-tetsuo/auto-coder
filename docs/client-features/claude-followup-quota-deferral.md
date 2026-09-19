# Claude follow-up quota deferral

Claude Routine follow-ups for review repair, adversarial correction feedback,
and merge-conflict repair preserve typed quota refusals as durable, work-specific
waits. A wait records repository, PR, backend, non-secret credential context,
owning task, operation, work identity, reason, delivery certainty, observation
time, and the provider-derived retry deadline.

Definitely-unsent work becomes eligible for authoritative reevaluation after its
deadline; a deadline is not evidence of provider recovery or permission to send.
Indeterminate delivery remains separate and is never replayed merely because a
quota deadline expired. Repeated processing before a retained deadline reports a
`DEFERRED` result without another assignment, while unrelated credentials and
providers remain available.
