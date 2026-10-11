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

Muse read-only calls now confirm host `allowAll` automatic authorization before
dispatching a turn, like implementation calls. A successful inspection appears
as a completed `llm.local-execution` with phase `read-only`, without a
`llm.muse-interactive-request` blocked stage. Explicit `--disable-approval`
still selects `denyUnmatched`; unexpected interactive requests and rejected
mutation still appear as failures. Neither automatic permission nor a completed
client call proves review approval. The mounted-detail regression
`test_muse_noedit_automatic_permission_reaches_mounted_detail` covers the success
path alongside the existing interactive-refusal negative control.

Run `bash scripts/test.sh tests/test_dashboard_observability.py tests/test_muse_msp.py`
for production dispatch-to-mounted-view coverage, admission/preparation negative
controls, interruption pairing, and executable Muse protocol coverage.

Explicit single-Issue local execution waits through publication. Its finalizer
emits `issue.local-publication-finalization` with origin `explicit-single-target`,
the exact job ID, disposition, PR number, and diagnostic. Published/no-change
results emit COMPLETED, unsuccessful terminal results emit FAILED, and pending
publication emits DEFERRED. Job acceptance remains ACCEPTED_HANDOFF and cannot
represent implementation completion. Run
`bash scripts/test.sh tests/test_explicit_local_issue_completion.py` for this
execution-to-finalization emission contract. The event schema and dashboard
renderer remain unchanged.

The `issue.local-completion` execution supplies the requested Issue identity for
command-owned finalization events and closes after finalization with the actual
Success, Failed, or Deferred outcome. It is separate from the dispatch scope that
ends at acceptance.

The explicit local completion regression also mounts the production dashboard
Issue detail route and asserts that local completion, publication finalization,
and their actual outcomes reach its diagram. Refused entry displays Deferred
without a publication-finalization stage.
