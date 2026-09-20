from auto_coder.dashboard_reviews import backend_summary, list_row, selection_error
from auto_coder.review_audit import (
    EvaluationLifecycle,
    ExecutionMode,
    ReviewAuditRecord,
    ReviewAuditStore,
    ReviewInteractionRecord,
    StorageHealth,
)


def record(review_id: str, number: str, sequence: int, kind: str = "issue_specification") -> ReviewAuditRecord:
    return ReviewAuditRecord(
        review_id=review_id,
        repository="owner/repo",
        target_type="issue",
        target_number=number,
        review_kind=kind,
        origin="production",
        process_identity="process-1",
        creation_time=f"2026-01-01T00:00:{sequence:02d}Z",
        creation_sequence=sequence,
        reviewed_generation=f"generation-{sequence}",
        policy_identity="policy-1",
        related_issue_membership=None,
        diagnostic_execution_references=None,
        lifecycle=EvaluationLifecycle.FINISHED,
        execution_mode=ExecutionMode.EXECUTED,
        native_verdict="READY",
        native_report={"identity": {"issue_number": number}, "findings": []},
        source_review_id=None,
    )


def test_history_filters_before_bounded_snapshot_page(tmp_path):
    store = ReviewAuditStore(tmp_path)
    for sequence in range(1, 55):
        assert store.record_evaluation(record(f"other-{sequence}", "99", sequence))
    expected = record("wanted", "42", 55)
    assert store.record_evaluation(expected)

    result = store.get_recent_history(
        "owner/repo",
        limit=1,
        target_type="issue",
        target_number="42",
        review_kind="issue_specification",
    )

    assert result.health is StorageHealth.AVAILABLE
    assert [item.review_id for item in result.records] == ["wanted"]


def test_captured_decomposition_membership_is_historical_and_repository_scoped(tmp_path):
    store = ReviewAuditStore(tmp_path)
    decomposition = record("family", "10", 1, "issue_decomposition")
    decomposition.native_report = {
        "identity": {
            "parent": {"issue_number": 10, "specification_digest": "parent"},
            "children": [{"issue_number": 42, "specification_digest": "child"}],
        }
    }
    assert store.record_evaluation(decomposition)
    assert ReviewAuditStore(tmp_path).record_evaluation(record("foreign", "42", 2))

    result = store.get_decomposition_evaluations_for_member("owner/repo", "42")

    assert result.health is StorageHealth.AVAILABLE
    assert [item.review_id for item in result.records] == ["family"]


def test_projection_does_not_conflate_requested_and_reported_model():
    item = record("review-1", "42", 1)
    item.interactions.append(
        ReviewInteractionRecord(
            interaction_id="interaction-1",
            review_id=item.review_id,
            start_time="2026-01-01T00:00:00Z",
            end_time="2026-01-01T00:00:01Z",
            duration_ms=1000,
            backend_alias="reviewer",
            backend_type="codex",
            provider_alias="openai",
            requested_model="requested-model",
            reported_model="reported-model",
            invocation_mode="cli",
            session_identity=None,
            completion_status="RETURNED",
        )
    )

    assert backend_summary(item) == "reviewer / requested requested-model / reported-model"
    assert list_row(item).detail_path == "/detail/issue/42?review_id=review-1"
    assert selection_error(item, "issue", 42) is None
    assert selection_error(item, "issue", 43) == "Review ID is not recorded for this repository and target; no other review was selected."
