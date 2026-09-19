# Claude follow-up quota admission

Claude Routine existing-session follow-ups use the repository-scoped configured
backend's Claude OAuth credential for both an uncached usage observation and the
subsequent Claude CLI assignment. Routine trigger credentials are not usage
credentials and never authorize this path.

Every nonempty follow-up obtains a new usage observation immediately before the
CLI call. Invalid, absent, rate-limited, or unreachable observations defer the
assignment rather than relying on the general display cache. Five-hour, weekly,
model-weekly, and extra-usage thresholds use the conservative follow-up limits.

Quota deferrals raise `ClaudeFollowupUsageLimitError`, which exposes the reason,
repository and backend identity, nonsecret credential context, observation and
retry timestamps, blocking evidence, and delivery certainty. Provider limit
diagnostics are inspected before a zero exit status can be accepted. Ordinary
failures retain the existing `False` result, while eligible successful sends
retain the existing `True` result and existing-session message behavior.
