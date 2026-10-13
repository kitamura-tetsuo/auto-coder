# Crash-safe Muse continuation recovery

Accepted local jobs retain the Muse invocation's absolute wall-clock deadline
with the provider session. Restarting a controller therefore consumes the
original invocation budget instead of granting another full timeout. Legacy
continuations without a trustworthy deadline remain reconciliation work and
cannot be resumed by inventing one.

`LocalJobRunner` discovers this work from the continuation store rather than
ordinary Issue admission. It first proves the recorded process owner is dead,
checks the retained workspace and upstream authority through the domain
adapter's recovery-specific predicate, and atomically replaces the executor
token. The replacement token fences delayed writes from the old executor and
ensures two recovering controllers cannot both advance the session.

Provider reconciliation is a domain-adapter boundary. Missing production
support is retained as an explicit reconciliation reason rather than leaving a
RUNNING job without a diagnostic. The Issue adapter can promote an exact Muse
output that was confirmed after writer settlement by rebuilding the ordinary
workspace manifest from the retained source commit and workspace; it never
creates another clone or invokes the implementation backend. The boundary receives
the exact retained provider session and only the remaining original budget.
The adapter must observe or resume that session and confirm its current result
frontier before a successful result can cross the existing artifact and
downstream-effect boundary. Missing support, an unavailable workspace, expired
budget, superseded authority, or an unresolved provider result leaves a
specific durable reconciliation reason; none causes a fresh implementation.

Run `bash scripts/test.sh tests/test_local_job_runner.py
tests/test_local_job_continuation.py` for dead-owner takeover, deadline,
executor fencing, positive recovered progress, and legacy fail-closed coverage.
