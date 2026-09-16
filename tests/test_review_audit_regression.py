import sqlite3
from pathlib import Path

import pytest

from auto_coder.review_audit import EvaluationLifecycle, ExecutionMode, ReviewAuditRecord, ReviewAuditStore, ReviewInteractionRecord, StorageHealth


def test_repository_isolation_collision(tmp_path):
    # AS/Counterexample: a/b and a_b mapping to the same db
    store = ReviewAuditStore(tmp_path)

    # Write to a/b
    rec1 = ReviewAuditRecord(
        review_id="rev-1",
        repository="a/b",
        target_type="pr",
        target_number="1",
        review_kind="pr_adversarial",
        origin="test",
        process_identity="p1",
        creation_time="time",
        creation_sequence=1,
        reviewed_generation="gen1",
        policy_identity="pol1",
        related_issue_membership=None,
        diagnostic_execution_references=None,
        lifecycle=EvaluationLifecycle.QUEUED,
        execution_mode=ExecutionMode.UNKNOWN,
        native_verdict=None,
        native_report=None,
        source_review_id=None,
    )
    assert store.record_evaluation(rec1)

    # Write to a_b
    rec2 = ReviewAuditRecord(
        review_id="rev-2",
        repository="a_b",
        target_type="pr",
        target_number="2",
        review_kind="pr_adversarial",
        origin="test",
        process_identity="p1",
        creation_time="time",
        creation_sequence=2,
        reviewed_generation="gen2",
        policy_identity="pol1",
        related_issue_membership=None,
        diagnostic_execution_references=None,
        lifecycle=EvaluationLifecycle.QUEUED,
        execution_mode=ExecutionMode.UNKNOWN,
        native_verdict=None,
        native_report=None,
        source_review_id=None,
    )
    assert store.record_evaluation(rec2)

    # Read from a/b -> should only get rev-1
    res1 = store.get_evaluation("a/b", "rev-1")
    assert res1.health == StorageHealth.AVAILABLE and res1.record is not None

    res_cross1 = store.get_evaluation("a/b", "rev-2")
    assert res_cross1.health == StorageHealth.AVAILABLE and res_cross1.record is None

    # Read from a_b -> should only get rev-2
    res2 = store.get_evaluation("a_b", "rev-2")
    assert res2.health == StorageHealth.AVAILABLE and res2.record is not None

    res_cross2 = store.get_evaluation("a_b", "rev-1")
    assert res_cross2.health == StorageHealth.AVAILABLE and res_cross2.record is None

    # Paths should be distinct
    path1 = store._get_db_path("a/b")
    path2 = store._get_db_path("a_b")
    assert path1 != path2


def test_terminal_overwrite_immutability(tmp_path):
    store = ReviewAuditStore(tmp_path)
    rec1 = ReviewAuditRecord(
        review_id="rev-t1",
        repository="repo",
        target_type="pr",
        target_number="1",
        review_kind="pr_adversarial",
        origin="test",
        process_identity="p1",
        creation_time="time",
        creation_sequence=1,
        reviewed_generation="gen1",
        policy_identity="pol1",
        related_issue_membership=None,
        diagnostic_execution_references=None,
        lifecycle=EvaluationLifecycle.FINISHED,
        execution_mode=ExecutionMode.EXECUTED,
        native_verdict="READY",
        native_report={"r": 1},
        source_review_id=None,
    )
    assert store.record_evaluation(rec1)

    # Update to BLOCKED should fail
    assert store.update_evaluation("rev-t1", "repo", EvaluationLifecycle.FINISHED, ExecutionMode.EXECUTED, "BLOCKED", {"r": 2}) is False

    # Record same gen with different verdict should fail
    rec2 = ReviewAuditRecord(
        review_id="rev-t1",
        repository="repo",
        target_type="pr",
        target_number="1",
        review_kind="pr_adversarial",
        origin="test",
        process_identity="p1",
        creation_time="time",
        creation_sequence=1,
        reviewed_generation="gen1",
        policy_identity="pol1",
        related_issue_membership=None,
        diagnostic_execution_references=None,
        lifecycle=EvaluationLifecycle.FINISHED,
        execution_mode=ExecutionMode.EXECUTED,
        native_verdict="BLOCKED",
        native_report={"r": 2},
        source_review_id=None,
    )
    assert store.record_evaluation(rec2) is False

    # Identical retry should succeed idempotently
    assert store.record_evaluation(rec1) is True

    # Result should still be READY and R1
    res = store.get_evaluation("repo", "rev-t1")
    assert res.record.native_verdict == "READY"
    assert res.record.native_report == {"r": 1}


def test_interaction_persistence_redaction(tmp_path):
    store = ReviewAuditStore(tmp_path)

    # Write a dummy evaluation first to satisfy foreign key (actually we didn't add FK, but good practice)
    rec1 = ReviewAuditRecord(
        review_id="rev-i1",
        repository="repo",
        target_type="pr",
        target_number="1",
        review_kind="pr",
        origin="t",
        process_identity="p",
        creation_time="t",
        creation_sequence=1,
        reviewed_generation="g",
        policy_identity="p",
        related_issue_membership=None,
        diagnostic_execution_references=None,
        lifecycle=EvaluationLifecycle.QUEUED,
        execution_mode=ExecutionMode.UNKNOWN,
        native_verdict=None,
        native_report=None,
        source_review_id=None,
    )
    store.record_evaluation(rec1)

    inter = ReviewInteractionRecord(
        interaction_id="int-1",
        review_id="rev-i1",
        start_time="time",
        end_time="time",
        duration_ms=10,
        backend_alias="alias",
        backend_type="type",
        provider_alias="prov",
        requested_model="model_ghp_1234567890abcdefGH",
        reported_model="report_sk-proj-xyz123",
        invocation_mode="api",
        session_identity="sess_SUPER_SECRET",
        completion_status="RETURNED",
    )

    # Store with credentials
    assert store.record_interaction("repo", inter, credentials=["SUPER_SECRET"]) is True

    # Inspect raw sqlite bytes/rows
    db_path = store._get_db_path("repo")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM interaction WHERE interaction_id = 'int-1'").fetchone()
    conn.close()

    assert row["requested_model"] == "model_[REDACTED]"
    assert row["reported_model"] == "report_[REDACTED]"
    assert row["session_identity"] == "sess_[REDACTED]"


def test_pagination_and_invalid_limit(tmp_path):
    store = ReviewAuditStore(tmp_path)
    # Insert 6 records
    for i in range(1, 7):
        rec = ReviewAuditRecord(
            review_id=f"rev-{i}",
            repository="repo",
            target_type="pr",
            target_number="1",
            review_kind="pr",
            origin="t",
            process_identity="p",
            creation_time="t",
            creation_sequence=i,
            reviewed_generation="g",
            policy_identity="p",
            related_issue_membership=None,
            diagnostic_execution_references=None,
            lifecycle=EvaluationLifecycle.QUEUED,
            execution_mode=ExecutionMode.UNKNOWN,
            native_verdict=None,
            native_report=None,
            source_review_id=None,
        )
        store.record_evaluation(rec)

    # HWM=5, get page 1 (limit 3) -> should get 5, 4, 3
    page1 = store.get_recent_history("repo", limit=3, high_water_mark_seq=5)
    assert [r.creation_sequence for r in page1.records] == [5, 4, 3]

    # Page 2: HWM=5, before_seq=3 (lowest from page 1), limit 3 -> should get 2, 1
    page2 = store.get_recent_history("repo", limit=3, high_water_mark_seq=5, before_seq=3)
    assert [r.creation_sequence for r in page2.records] == [2, 1]

    # Invalid limit should return UNAVAILABLE
    res_inv1 = store.get_recent_history("repo", limit=0)
    assert res_inv1.health == StorageHealth.UNAVAILABLE

    res_inv2 = store.get_recent_history("repo", limit=201)
    assert res_inv2.health == StorageHealth.UNAVAILABLE
