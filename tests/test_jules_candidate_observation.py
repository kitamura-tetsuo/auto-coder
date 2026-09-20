from pathlib import Path

from auto_coder.jules_candidate_observation import (
    ArtifactClassification,
    CandidateObservation,
    CandidateObservationStore,
    EvidenceStatus,
    JulesCandidateObservationAdapter,
    ProviderState,
    PullRequestIdentity,
    VerifiedPullRequest,
    normalize_pull_request_outputs,
)
from auto_coder.jules_competition_ledger import (
    JulesCompetitionLedger,
    SpeculativeGenerationBundle,
)

REPO = "owner/repo"
ISSUE = 2072


class SessionReader:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get_session(self, session_id):
        self.calls.append(session_id)
        response = self.responses[session_id]
        if isinstance(response, Exception):
            raise response
        return response


class GitHubReader:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get_pull_request_metadata_strict(self, repository, number):
        self.calls.append((repository, number))
        response = self.responses[number]
        if isinstance(response, Exception):
            raise response
        return response


def pr(number, head_ref=None, base_ref="main", repository=REPO):
    return {
        "number": number,
        "head": {
            "repo": {"full_name": repository},
            "ref": head_ref or f"candidate-{number}",
            "sha": f"head-{number}",
        },
        "base": {
            "repo": {"full_name": REPO},
            "ref": base_ref,
            "sha": "base-sha",
        },
    }


def accepted_ledger(tmp_path: Path, candidates=("a",)):
    ledger = JulesCompetitionLedger(tmp_path / "ledger.db")
    created = ledger.create_generation(
        REPO,
        ISSUE,
        "create",
        0,
        SpeculativeGenerationBundle(
            source_attempt_number=1,
            candidate_ids=candidates,
            issue_oracle_snapshot="snapshot",
            issue_oracle_fingerprint="fingerprint",
            source_branch="main",
        ),
    )
    generation = created.generation_id
    assert generation is not None
    epoch = created.snapshot.epoch
    for candidate in candidates:
        claimed = ledger.claim_candidate_submission(REPO, ISSUE, generation, candidate, f"claim-{candidate}", epoch)
        accepted = ledger.record_candidate_accepted(
            REPO,
            ISSUE,
            generation,
            candidate,
            f"accept-{candidate}",
            claimed.snapshot.epoch,
            "jules",
            f"session-{candidate}",
        )
        epoch = accepted.snapshot.epoch
    return ledger, generation


def test_normalizer_preserves_all_variants_and_reports_malformed():
    outputs = [
        {"pullRequest": "https://github.com/owner/repo/pull/10"},
        ["pull_request", {"number": 11, "repository": {"full_name": REPO}}],
        {"pullRequest": "https://github.com/owner/repo/pull/10"},
        {"pullRequest": {"number": "bad"}},
        "broken",
    ]

    identities, malformed = normalize_pull_request_outputs(outputs)

    assert identities == (
        PullRequestIdentity(REPO, 10),
        PullRequestIdentity(REPO, 11),
    )
    assert malformed == 2


def test_adapter_verifies_every_pr_and_marks_malformed_output_incomplete(tmp_path):
    ledger, generation = accepted_ledger(tmp_path)
    sessions = SessionReader(
        {
            "session-a": {
                "name": "projects/p/locations/l/sessions/session-a",
                "sourceContext": {"source": f"sources/github/{REPO}"},
                "state": "AWAITING_USER_FEEDBACK",
                "outputs": [
                    {"pullRequest": f"https://github.com/{REPO}/pull/10"},
                    {"pull_request": {"number": 11, "repository": {"name": REPO}}},
                    {"pullRequest": {"number": None}},
                ],
            }
        }
    )
    github = GitHubReader({10: pr(10), 11: pr(11)})
    store = CandidateObservationStore(tmp_path / "observations.db")
    adapter = JulesCandidateObservationAdapter(ledger, store, sessions, github)

    observation = adapter.observe_candidate(REPO, ISSUE, generation, "a")

    assert observation.provider_state is ProviderState.AWAITING_USER_FEEDBACK
    assert observation.evidence_status is EvidenceStatus.INCOMPLETE
    assert observation.malformed_outputs == 1
    assert [artifact.identity.number for artifact in observation.artifacts] == [10, 11]
    assert github.calls == [(REPO, 10), (REPO, 11)]
    assert observation.published is True
    assert adapter.classify(REPO, ISSUE, REPO, 10).classification is ArtifactClassification.BLOCKED


def test_unavailable_read_retains_membership_and_does_not_call_github(tmp_path):
    ledger, generation = accepted_ledger(tmp_path)
    response = {
        "name": "sessions/session-a",
        "sourceContext": {"source": f"sources/github/{REPO}"},
        "state": "COMPLETED",
        "outputs": {"pullRequest": f"https://github.com/{REPO}/pull/10"},
    }
    sessions = SessionReader({"session-a": response})
    github = GitHubReader({10: pr(10)})
    store = CandidateObservationStore(tmp_path / "observations.db")
    adapter = JulesCandidateObservationAdapter(ledger, store, sessions, github)
    assert adapter.observe_candidate(REPO, ISSUE, generation, "a").evidence_status is EvidenceStatus.KNOWN
    sessions.responses["session-a"] = RuntimeError("404")

    unavailable = adapter.observe_candidate(REPO, ISSUE, generation, "a")

    assert unavailable.evidence_status is EvidenceStatus.UNAVAILABLE
    assert github.calls == [(REPO, 10)]
    classified = adapter.classify(REPO, ISSUE, REPO, 10)
    assert classified.classification is ArtifactClassification.BLOCKED
    assert classified.mutation_allowed is False


def test_stale_read_cannot_publish_after_invalidation(tmp_path):
    store = CandidateObservationStore(tmp_path / "observations.db")
    scope = store.scope(REPO, ISSUE, "generation", "a")
    old_read = store.begin_read(scope)
    invalidation = store.invalidate(scope)
    observation = CandidateObservation(
        REPO,
        ISSUE,
        "generation",
        "a",
        read_id=old_read,
        evidence_status=EvidenceStatus.KNOWN,
        artifacts=(VerifiedPullRequest(PullRequestIdentity(REPO, 10)),),
    )

    assert invalidation > old_read
    assert store.publish(scope, observation) is False
    assert store.memberships(REPO, ISSUE) == ()


def test_shared_writable_head_blocks_both_candidates(tmp_path):
    ledger, generation = accepted_ledger(tmp_path, ("a", "b"))
    sessions = SessionReader(
        {
            session_id: {
                "name": f"sessions/{session_id}",
                "sourceContext": {"source": f"sources/github/{REPO}"},
                "state": "IN_PROGRESS",
                "outputs": {"pullRequest": f"https://github.com/{REPO}/pull/{number}"},
            }
            for session_id, number in (("session-a", 10), ("session-b", 11))
        }
    )
    github = GitHubReader({10: pr(10, "shared"), 11: pr(11, "shared")})
    adapter = JulesCandidateObservationAdapter(ledger, CandidateObservationStore(tmp_path / "observations.db"), sessions, github)
    adapter.observe_candidate(REPO, ISSUE, generation, "a")
    adapter.observe_candidate(REPO, ISSUE, generation, "b")

    first = adapter.classify(REPO, ISSUE, REPO, 10)
    second = adapter.classify(REPO, ISSUE, REPO, 11)
    assert first.classification is ArtifactClassification.BLOCKED
    assert second.classification is ArtifactClassification.BLOCKED
    assert not first.mutation_allowed and not first.cleanup_allowed


def test_foreign_or_wrong_base_never_becomes_membership(tmp_path):
    ledger, generation = accepted_ledger(tmp_path)
    sessions = SessionReader(
        {
            "session-a": {
                "name": "sessions/session-a",
                "sourceContext": {"source": f"sources/github/{REPO}"},
                "state": "COMPLETED",
                "outputs": [
                    {"pullRequest": "https://github.com/foreign/repo/pull/10"},
                    {"pullRequest": f"https://github.com/{REPO}/pull/11"},
                ],
            }
        }
    )
    adapter = JulesCandidateObservationAdapter(
        ledger,
        CandidateObservationStore(tmp_path / "observations.db"),
        sessions,
        GitHubReader({11: pr(11, base_ref="develop")}),
    )

    observation = adapter.observe_candidate(REPO, ISSUE, generation, "a")

    assert observation.evidence_status is EvidenceStatus.INCOMPLETE
    assert observation.artifacts == ()
    assert adapter.classify(REPO, ISSUE, REPO, 11).classification is ArtifactClassification.SUSPECTED
