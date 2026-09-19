"""Production-boundary regressions for crash-safe Jules candidate submission."""

from pathlib import Path
from unittest.mock import Mock

from auto_coder.jules_candidate_submission import (
    CandidateRequest,
    CandidateSubmissionOutcome,
    JulesCandidateSubmissionAdapter,
)
from auto_coder.jules_client import JulesSessionOutcomeUncertainError, JulesSessionRejectedError
from auto_coder.jules_competition_ledger import JulesCompetitionLedger, SpeculativeGenerationBundle

REPO = "owner/repo"
ISSUE = 2071


def _adapter(tmp_path: Path, client: Mock, candidates=("a", "b")) -> tuple[JulesCandidateSubmissionAdapter, JulesCompetitionLedger, str]:
    ledger = JulesCompetitionLedger(tmp_path / "competition.db")
    created = ledger.create_generation(
        REPO,
        ISSUE,
        "create",
        0,
        SpeculativeGenerationBundle(
            source_attempt_number=1,
            candidate_ids=candidates,
            issue_oracle_snapshot="captured issue and oracle",
            issue_oracle_fingerprint="fingerprint",
            source_branch="main",
        ),
    )
    assert created.generation_id
    return JulesCandidateSubmissionAdapter(ledger, client, tmp_path / "submissions.db"), ledger, created.generation_id


def _request(generation_id: str, candidate: str = "a") -> CandidateRequest:
    return CandidateRequest(REPO, ISSUE, generation_id, candidate, f"{REPO}#{ISSUE}", "captured issue and oracle")


def test_claim_suppresses_repeat_and_restart_and_prompt_is_isolated(tmp_path: Path) -> None:
    client = Mock()
    client.start_session.return_value = "session-a"
    adapter, ledger, generation_id = _adapter(tmp_path, client)

    first = adapter.submit(_request(generation_id))
    restarted = JulesCandidateSubmissionAdapter(ledger, client, tmp_path / "submissions.db")
    second = restarted.submit(_request(generation_id))

    assert first.outcome == CandidateSubmissionOutcome.ACCEPTED
    assert second.outcome == CandidateSubmissionOutcome.ACCEPTED
    assert second.session_id == "session-a"
    client.start_session.assert_called_once()
    prompt = client.start_session.call_args.args[0]
    assert first.correlation_marker in prompt
    assert "Publish your own independent pull request" in prompt
    assert "Do not merge any pull request, close the source Issue" in prompt
    assert client.start_session.call_args.args[1:3] == (REPO, "main")


def test_uncertain_and_definite_rejection_are_both_terminal_for_send(tmp_path: Path) -> None:
    uncertain_client = Mock()
    uncertain_client.start_session.side_effect = JulesSessionOutcomeUncertainError("lost response")
    adapter, ledger, generation_id = _adapter(tmp_path, uncertain_client)
    assert adapter.submit(_request(generation_id)).outcome == CandidateSubmissionOutcome.UNKNOWN
    assert adapter.submit(_request(generation_id)).outcome == CandidateSubmissionOutcome.BLOCKED
    uncertain_client.start_session.assert_called_once()

    rejected_client = Mock()
    rejected_client.start_session.side_effect = JulesSessionRejectedError("invalid request")
    adapter2, _, generation_id2 = _adapter(tmp_path / "rejected", rejected_client)
    assert adapter2.submit(_request(generation_id2)).outcome == CandidateSubmissionOutcome.DEFINITELY_NOT_ACCEPTED
    assert adapter2.submit(_request(generation_id2)).outcome == CandidateSubmissionOutcome.BLOCKED
    rejected_client.start_session.assert_called_once()


def test_exact_reconciliation_recovers_and_quarantines_ambiguity(tmp_path: Path) -> None:
    client = Mock()
    client.start_session.side_effect = JulesSessionOutcomeUncertainError("lost")
    adapter, _, generation_id = _adapter(tmp_path, client)
    request = _request(generation_id)
    unknown = adapter.submit(request)
    marker = unknown.correlation_marker
    prompt = client.start_session.call_args.args[0]

    unrelated = {"id": "wrong", "prompt": prompt, "source": "sources/github/other/repo", "startingBranch": "main"}
    assert adapter.reconcile(request, [unrelated]).outcome == CandidateSubmissionOutcome.UNKNOWN

    exact = {"name": "sessions/recovered", "prompt": prompt, "source": f"sources/github/{REPO}", "startingBranch": "main"}
    recovered = adapter.reconcile(request, [exact])
    assert recovered.outcome == CandidateSubmissionOutcome.ACCEPTED
    assert recovered.session_id == "recovered"
    assert recovered.correlation_marker == marker

    adapter2, _, generation_id2 = _adapter(tmp_path / "ambiguous", Mock(), candidates=("a",))
    request2 = _request(generation_id2)
    saved = adapter2._persist_request(request2, "main")
    prompt2 = adapter2._candidate_prompt(saved)
    sessions = [{"id": sid, "prompt": prompt2, "source": f"sources/github/{REPO}", "startingBranch": "main"} for sid in ("one", "two")]
    blocked = adapter2.reconcile(request2, sessions)
    assert blocked.outcome == CandidateSubmissionOutcome.BLOCKED
    assert blocked.observed_session_ids == ("one", "two")


def test_retired_candidate_never_crosses_send_boundary(tmp_path: Path) -> None:
    client = Mock()
    adapter, ledger, generation_id = _adapter(tmp_path, client)
    snapshot = ledger.get_namespace_snapshot(REPO, ISSUE)
    ledger.retire_candidate(REPO, ISSUE, generation_id, "a", "retire-a", snapshot.epoch, "sibling won")

    result = adapter.submit(_request(generation_id))

    assert result.outcome == CandidateSubmissionOutcome.BLOCKED
    client.start_session.assert_not_called()
