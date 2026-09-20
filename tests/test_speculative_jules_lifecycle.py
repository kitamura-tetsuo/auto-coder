from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from auto_coder.automation_config import AutomationConfig, PRProcessingOutcome
from auto_coder.jules_candidate_observation import ArtifactClassification, ClassificationResult
from auto_coder.jules_competition_ledger import ObligationKind
from auto_coder.pr_processor import process_pull_request
from auto_coder.speculative_jules_lifecycle import (
    CleanupObligation,
    RefreshingSpeculativeClassifier,
    SpeculativeCleanupStore,
    SpeculativeJulesLifecycle,
    configure_speculative_jules_lifecycle,
    consume_due_speculative_work,
)


class Classifier:
    def __init__(self, result: ClassificationResult, issue_number: int = 7, selected_pr: int | None = None):
        self.result = result
        self.issue_number = issue_number
        self.selected_pr = selected_pr
        self.calls = 0

    def classify(self, repository: str, issue_number: int, pr_repository: str, pr_number: int) -> ClassificationResult:
        self.calls += 1
        return self.result

    def classify_pr(self, repository: str, pr_repository: str, pr_number: int, hinted_issue_numbers: tuple[int, ...] = ()) -> tuple[int, ClassificationResult]:
        self.calls += 1
        return self.issue_number, self.result

    def selected_pr_number(self, repository: str, issue_number: int, generation_id: str) -> int | None:
        return self.selected_pr


def lifecycle(tmp_path: Path, result: ClassificationResult) -> tuple[SpeculativeJulesLifecycle, Classifier]:
    classifier = Classifier(result)
    return SpeculativeJulesLifecycle(classifier, SpeculativeCleanupStore(tmp_path / "cleanup.db")), classifier


def test_targeted_maintenance_refreshes_known_sessions_without_account_listing(tmp_path: Path) -> None:
    adapter = Mock()
    candidate = SimpleNamespace(candidate_id="c1", session_id="session-1")
    generation = SimpleNamespace(generation_id="g1", candidates=(candidate,))
    ledger = Mock()
    ledger.list_issue_numbers.return_value = (7,)
    ledger.get_namespace_snapshot.return_value = SimpleNamespace(generations=(generation,))
    ledger.list_pending_obligations.return_value = ()
    classifier = RefreshingSpeculativeClassifier(adapter, ledger)
    service = SpeculativeJulesLifecycle(classifier, SpeculativeCleanupStore(tmp_path / "cleanup.db"))
    configure_speculative_jules_lifecycle(service)
    try:
        assert consume_due_speculative_work("owner/repo", Mock()) == (1, 0)
    finally:
        configure_speculative_jules_lifecycle(None)
    adapter.observe_candidate.assert_called_once_with("owner/repo", 7, "g1", "c1")
    adapter.list_sessions.assert_not_called()


def test_targeted_maintenance_delivers_durable_aggregate_failure(tmp_path: Path) -> None:
    adapter = Mock()
    ledger = Mock()
    ledger.list_issue_numbers.return_value = (7,)
    ledger.get_namespace_snapshot.return_value = SimpleNamespace(generations=())
    obligation = SimpleNamespace(kind=ObligationKind.AGGREGATE_FAILURE, obligation_id="obl-1")
    ledger.list_pending_obligations.return_value = (obligation,)
    classifier = RefreshingSpeculativeClassifier(adapter, ledger)
    service = SpeculativeJulesLifecycle(classifier, SpeculativeCleanupStore(tmp_path / "cleanup.db"))
    enqueue_issue = Mock()
    configure_speculative_jules_lifecycle(service)
    try:
        assert consume_due_speculative_work("owner/repo", Mock(), enqueue_issue=enqueue_issue) == (0, 0)
    finally:
        configure_speculative_jules_lifecycle(None)
    enqueue_issue.assert_called_once_with(7)
    ledger.mark_obligation_delivered.assert_called_once_with("obl-1", "production-deliver:obl-1")
    ledger.acknowledge_obligation.assert_called_once_with("obl-1", "production-ack:obl-1")


def test_active_candidate_is_deferred_without_cleanup(tmp_path: Path) -> None:
    service, _ = lifecycle(tmp_path, ClassificationResult(ArtifactClassification.ACTIVE_UNSELECTED, "c1", "g1"))
    decision = service.evaluate_pr("owner/repo", 41)
    assert decision.allow_ordinary_processing is False
    assert decision.cleanup_pending is False
    assert service.cleanup_store.pending() == ()


def test_retirement_is_durable_before_cleanup_and_survives_restart(tmp_path: Path) -> None:
    result = ClassificationResult(ArtifactClassification.RETIRED, "c2", "g1", cleanup_allowed=True)
    service, _ = lifecycle(tmp_path, result)
    assert service.evaluate_pr("owner/repo", 42).cleanup_pending is True
    restarted = SpeculativeCleanupStore(tmp_path / "cleanup.db")
    assert restarted.pending() == (CleanupObligation("owner/repo", 7, "g1", "c2", 42, None, "retired Jules competitor in generation g1"),)


def test_cleanup_rechecks_authority_and_confirms_close(tmp_path: Path) -> None:
    result = ClassificationResult(ArtifactClassification.RETIRED, "c2", "g1", cleanup_allowed=True)
    service, classifier = lifecycle(tmp_path, result)
    service.evaluate_pr("owner/repo", 42)
    github = Mock()
    github.get_pull_request_metadata_strict.side_effect = [{"number": 42, "state": "open"}, {"number": 42, "state": "closed"}]
    assert service.consume_cleanup(github) == 1
    assert classifier.calls == 3
    github.close_pr.assert_called_once_with("owner/repo", 42, "Auto-Coder: Closing verified losing Jules candidate PR. retired Jules competitor in generation g1.")
    assert service.cleanup_store.pending() == ()


def test_cleanup_conflict_at_mutation_boundary_keeps_obligation(tmp_path: Path) -> None:
    retired = ClassificationResult(ArtifactClassification.RETIRED, "c2", "g1", cleanup_allowed=True)
    service, classifier = lifecycle(tmp_path, retired)
    service.evaluate_pr("owner/repo", 42)
    classifier.result = retired
    github = Mock()
    github.get_pull_request_metadata_strict.return_value = {"number": 42, "state": "open"}

    original = classifier.classify

    def changing(*args: object) -> ClassificationResult:
        if classifier.calls >= 2:
            return ClassificationResult(ArtifactClassification.BLOCKED, "c2", "g1")
        return original(*args)

    classifier.classify = changing  # type: ignore[method-assign]

    assert service.consume_cleanup(github) == 0
    github.close_pr.assert_not_called()
    assert len(service.cleanup_store.pending()) == 1


def test_top_level_processor_fences_retired_empty_pr_before_lifecycle_effects(tmp_path: Path) -> None:
    retired = ClassificationResult(ArtifactClassification.RETIRED, "c2", "g1", cleanup_allowed=True)
    service, _ = lifecycle(tmp_path, retired)
    github = Mock()
    pr = {
        "number": 42,
        "state": "open",
        "changed_files": 0,
        "body": "Closes #7",
        "head": {"sha": "abc", "ref": "issue-7"},
    }
    configure_speculative_jules_lifecycle(service)
    try:
        result = process_pull_request(github, AutomationConfig(), "owner/repo", pr, force_adversarial_validation=True)
    finally:
        configure_speculative_jules_lifecycle(None)

    assert result.outcome is PRProcessingOutcome.DEFERRED
    assert result.priority == "cleanup"
    assert result.actions_taken == ["Speculative Jules retired artifact fenced: verified retired Jules competitor"]
    github.close_pr.assert_not_called()


def test_top_level_processor_fences_unlinked_retired_pr(tmp_path: Path) -> None:
    retired = ClassificationResult(ArtifactClassification.RETIRED, "c2", "g1", cleanup_allowed=True)
    service, _ = lifecycle(tmp_path, retired)
    github = Mock()
    pr = {"number": 42, "state": "open", "changed_files": 0, "body": "", "head": {"sha": "abc", "ref": "jules-output"}}
    configure_speculative_jules_lifecycle(service)
    try:
        result = process_pull_request(github, AutomationConfig(), "owner/repo", pr)
    finally:
        configure_speculative_jules_lifecycle(None)

    assert result.outcome is PRProcessingOutcome.DEFERRED
    assert result.priority == "cleanup"
    github.close_pr.assert_not_called()


def test_cleanup_reason_includes_selected_pr_when_known(tmp_path: Path) -> None:
    result = ClassificationResult(ArtifactClassification.RETIRED, "c2", "g1", cleanup_allowed=True)
    classifier = Classifier(result, selected_pr=41)
    service = SpeculativeJulesLifecycle(classifier, SpeculativeCleanupStore(tmp_path / "cleanup.db"))
    service.evaluate_pr("owner/repo", 42)
    assert service.cleanup_store.pending()[0].reason == "retired Jules competitor in generation g1; selected PR is #41"


def test_jules_maintenance_drives_due_cleanup_without_new_issue_event() -> None:
    github = Mock()
    cleanup = Mock(return_value=1)
    with (
        patch("auto_coder.jules_engine.JulesClient") as jules_client,
        patch("auto_coder.jules_engine.GitHubClient.get_instance", return_value=github),
        patch("auto_coder.speculative_jules_lifecycle.consume_due_speculative_cleanup", cleanup),
    ):
        jules_client.return_value.list_sessions.return_value = []
        from auto_coder.jules_engine import check_and_resume_or_archive_sessions

        check_and_resume_or_archive_sessions("owner/repo")

    cleanup.assert_called_once_with(github)
