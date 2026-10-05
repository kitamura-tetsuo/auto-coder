# Local LLM execution observability

BackendManager emits `llm.local-execution` immediately before dispatching a local
client call, after admission, private-workspace preparation, initial tests, and
execution-boundary setup. It applies to local backend aliases and both fresh and
explicit continuation calls, including implementation and read-only review.
Cloud backend calls do not produce this local stage. Preparation or admission
failures do not claim that a local call started.

The dashboard detail diagram and decision log show `Local LLM call: <alias>`
under the caller's existing Issue/PR execution identity while the call is still
pending. A matching INFO log says `Local LLM call started` and records the
repository, target, execution ID, backend alias/type, provider, model, invocation
ID, invocation mode, and implementation/read-only phase. No prompt, response,
raw command, or provider error text is included.

A normal client return emits a completed stage result; a raised error or
interruption emits a failed result with only the exception class. These outcomes
describe the client call, not result handoff, publication, test success, or merge
permission. A start proves controller dispatch, not provider process startup,
ongoing provider activity, or file edits. Missing completion after a process
crash remains an incomplete observation rather than evidence of liveness.

The anonymous `/api/logs` route exposes the start and result through its existing
fact allowlist (including backend, provider, phase, and error). The full diagnostic
facts remain in the local dashboard; no HTTP schema or fact allowlist changes.

Run `bash scripts/test.sh tests/test_dashboard_observability.py tests/test_muse_msp.py`
for production dispatch-to-mounted-view coverage, admission/preparation negative
controls, interruption pairing, and executable Muse protocol coverage.
