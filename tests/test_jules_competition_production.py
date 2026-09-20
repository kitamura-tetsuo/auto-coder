from types import SimpleNamespace
from unittest.mock import Mock, patch

from auto_coder.issue_processor import _dispatch_jules_competition, _process_issue_jules_mode
from auto_coder.jules_candidate_submission import CandidateSubmissionOutcome, CandidateSubmissionResult
from auto_coder.jules_competition_ledger import GenerationRetirementSource, JulesCompetitionLedger


def config(width: object = 3, validation: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        JULES_SPECULATIVE_PARALLELISM=width,
        JULES_ISSUE_PR_TIMEOUT_HOURS=12,
        JULES_PR_CI_TIMEOUT_HOURS=12,
        MAIN_BRANCH="main",
        pr_adversarial_validation=validation,
    )


@patch("auto_coder.issue_processor.JulesClient")
def test_invalid_runtime_width_is_reported_before_provider_dispatch(client_class: Mock) -> None:
    actions = _process_issue_jules_mode("owner/repo", {"number": 7, "title": "T"}, config(True), Mock())  # type: ignore[arg-type]
    assert actions == ["Error processing issue #7 in Jules mode: [jules].speculative_parallelism must be a positive integer (booleans are not valid)"]
    client_class.assert_not_called()


def test_disabled_validator_defers_before_generation_or_post(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    client = Mock()
    actions = _dispatch_jules_competition("owner/repo", {"number": 7}, config(validation=False), "task", client, None, None)  # type: ignore[arg-type]
    assert actions == ["Deferred Jules competition for issue #7: independent PR adversarial validation is disabled"]
    client.start_session.assert_not_called()
    assert JulesCompetitionLedger().get_namespace_snapshot("owner/repo", 7).generations == ()


@patch("auto_coder.cli_helpers.create_adversarial_validation_backend_manager", return_value=Mock())
@patch("auto_coder.issue_processor.get_current_attempt", return_value=1)
def test_retirement_stops_unsent_candidates(_attempt: Mock, _validator: Mock, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    adapter = Mock()

    def submit(request):
        ledger = JulesCompetitionLedger()
        snapshot = ledger.get_namespace_snapshot(request.repository, request.issue_number)
        ledger.retire_generation(
            request.repository,
            request.issue_number,
            request.generation_id,
            "test-retire",
            snapshot.epoch,
            "shutdown",
            GenerationRetirementSource.OPERATOR,
        )
        return CandidateSubmissionResult(CandidateSubmissionOutcome.ACCEPTED, session_id="session-1")

    adapter.submit.side_effect = submit
    with patch("auto_coder.jules_candidate_submission.JulesCandidateSubmissionAdapter", return_value=adapter):
        actions = _dispatch_jules_competition("owner/repo", {"number": 7}, config(), "task", Mock(), None, None)  # type: ignore[arg-type]
    assert adapter.submit.call_count == 1
    assert "requested=3, accepted=1" in actions[0]
