# Crash-safe Jules candidate submission

`JulesCandidateSubmissionAdapter` is the inactive-until-fan-out transport
boundary for candidates in a persisted Jules speculative generation. It
validates the active generation and fixed candidate identity, persists an
exact repository/generation/candidate request record and collision-resistant
correlation marker, and then acquires the generation ledger's durable
candidate claim before performing one external create request. Existing
claims, accepted sessions, unknown outcomes, definite rejections, retired
candidates, stale generation state, and storage failures all suppress a
second request.

Every outgoing candidate prompt retains the captured task payload and common
source repository/branch while adding the candidate identity, correlation
marker, and explicit isolation rules: Jules must publish its own PR from an
independent candidate branch and must not reuse sibling work, merge a PR,
close the Issue, or modify sibling artifacts. The legacy singleton Jules
dispatch path does not call this adapter and is unchanged.

The Jules response decoder accepts only a non-empty canonical `id`, a
`name` shaped as `sessions/<id>`, or an agreeing pair. Malformed, absent, or
conflicting identities and invalid JSON are unknown acceptance outcomes;
they never produce a synthetic session ID. HTTP retry configuration excludes
POST, because a response-lost create cannot safely be replayed.

Unknown outcomes can be reconciled only from supplied authenticated Session
observations whose canonical identity, exact correlation marker, source
repository, starting branch, and captured payload all match. No match stays
unknown. Multiple identities or malformed contradictory identity evidence is
blocked and returned for quarantine. A unique match is written to the
competition ledger before the adapter reports an accepted handoff, including
when the candidate was retired while its request was in flight.

Run
`bash scripts/test.sh tests/test_jules_candidate_submission.py tests/test_jules_client.py tests/test_cloud_provider_instructions_integration.py`
for the transport boundary and production request-decoder coverage.
