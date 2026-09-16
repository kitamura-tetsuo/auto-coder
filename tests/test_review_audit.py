import multiprocessing
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

from auto_coder.review_audit import EvaluationLifecycle, ExecutionMode, ReviewAuditRecord, ReviewAuditStore, ReviewEffectRecord, ReviewInteractionRecord, redact_sensitive_data


def test_as001_successful_records_survive_fresh_process(tmp_path):
    # AS-001 - Successful records survive a fresh process
    store1 = ReviewAuditStore(tmp_path)

    record = ReviewAuditRecord(
        review_id="rev-001",
        repository="test/repo",
        target_type="issue",
        target_number="42",
        review_kind="issue_specification",
        origin="test",
        process_identity="proc-1",
        creation_time="2023-10-27T10:00:00Z",
        creation_sequence=1,
        reviewed_generation="gen-1",
        policy_identity="pol-1",
        related_issue_membership=None,
        diagnostic_execution_references=None,
        lifecycle=EvaluationLifecycle.FINISHED,
        execution_mode=ExecutionMode.EXECUTED,
        native_verdict="READY",
        native_report={"summary": "all good"},
        source_review_id=None,
    )

    assert store1.record_evaluation(record)

    inter = ReviewInteractionRecord(
        interaction_id="int-001",
        review_id="rev-001",
        start_time="2023-10-27T10:00:01Z",
        end_time="2023-10-27T10:00:05Z",
        duration_ms=4000,
        backend_alias="claude",
        backend_type="claude",
        provider_alias="anthropic",
        requested_model="claude-3",
        reported_model="claude-3",
        invocation_mode="api",
        session_identity="sess-1",
        completion_status="RETURNED",
    )
    assert store1.record_interaction("test/repo", inter)

    # Fresh process simulation
    store2 = ReviewAuditStore(tmp_path)
    fetched_res = store2.get_evaluation("test/repo", "rev-001")

    assert fetched_res.health == StorageHealth.AVAILABLE
    fetched = fetched_res.record
    assert fetched is not None
    assert fetched.review_id == "rev-001"
    assert fetched.native_verdict == "READY"
    assert fetched.native_report == {"summary": "all good"}
    assert len(fetched.interactions) == 1
    assert fetched.interactions[0].interaction_id == "int-001"


def test_as002_reuse_is_new_observation(tmp_path):
    # AS-002 - Reuse is a new observation
    store = ReviewAuditStore(tmp_path)
    record = ReviewAuditRecord(
        review_id="rev-002",
        repository="test/repo",
        target_type="issue",
        target_number="43",
        review_kind="issue_specification",
        origin="test",
        process_identity="proc-1",
        creation_time="2023-10-27T10:10:00Z",
        creation_sequence=2,
        reviewed_generation="gen-2",
        policy_identity="pol-1",
        related_issue_membership=None,
        diagnostic_execution_references=None,
        lifecycle=EvaluationLifecycle.FINISHED,
        execution_mode=ExecutionMode.REUSED,
        native_verdict="READY",
        native_report=None,
        source_review_id="rev-001",
    )
    assert store.record_evaluation(record)

    fetched_res = store.get_evaluation("test/repo", "rev-002")
    fetched = fetched_res.record
    assert fetched.execution_mode == ExecutionMode.REUSED
    assert fetched.source_review_id == "rev-001"
    assert len(fetched.interactions) == 0


def test_as003_interrupted_recording_preserves_uncertainty(tmp_path):
    # AS-003 - Interrupted recording preserves uncertainty
    store = ReviewAuditStore(tmp_path)
    record = ReviewAuditRecord(
        review_id="rev-003",
        repository="test/repo",
        target_type="issue",
        target_number="44",
        review_kind="issue_specification",
        origin="test",
        process_identity="proc-1",
        creation_time="2023-10-27T10:20:00Z",
        creation_sequence=3,
        reviewed_generation="gen-3",
        policy_identity="pol-1",
        related_issue_membership=None,
        diagnostic_execution_references=None,
        lifecycle=EvaluationLifecycle.RUNNING,
        execution_mode=ExecutionMode.UNKNOWN,
        native_verdict=None,
        native_report=None,
        source_review_id=None,
    )
    assert store.record_evaluation(record)

    fetched_res = store.get_evaluation("test/repo", "rev-003")
    fetched = fetched_res.record
    assert fetched.lifecycle == EvaluationLifecycle.RUNNING

    # Try to overwrite with FINISHED but DIFFERENT generation (should fail)
    record.reviewed_generation = "gen-different"
    record.lifecycle = EvaluationLifecycle.FINISHED
    assert store.record_evaluation(record) is False


def concurrent_writer(db_path, repo, rev_id, seq, t_type, t_num):
    store = ReviewAuditStore(db_path)
    record = ReviewAuditRecord(
        review_id=rev_id,
        repository=repo,
        target_type=t_type,
        target_number=t_num,
        review_kind="issue_specification",
        origin="test",
        process_identity="proc-concurrent",
        creation_time="2023-10-27T10:30:00Z",
        creation_sequence=seq,
        reviewed_generation="gen-concurrent",
        policy_identity="pol-1",
        related_issue_membership=None,
        diagnostic_execution_references=None,
        lifecycle=EvaluationLifecycle.QUEUED,
        execution_mode=ExecutionMode.UNKNOWN,
        native_verdict=None,
        native_report=None,
        source_review_id=None,
    )
    store.record_evaluation(record)


def test_as004_concurrent_writes(tmp_path):
    # AS-004 - Concurrent and delayed writes
    db_path = str(tmp_path)

    p1 = multiprocessing.Process(target=concurrent_writer, args=(db_path, "test/repo1", "rev-c1", 10, "issue", "100"))
    p2 = multiprocessing.Process(target=concurrent_writer, args=(db_path, "test/repo2", "rev-c2", 11, "issue", "101"))

    p1.start()
    p2.start()
    p1.join()
    p2.join()

    store = ReviewAuditStore(db_path)
    assert store.get_evaluation("test/repo1", "rev-c1").record is not None
    assert store.get_evaluation("test/repo2", "rev-c2").record is not None


def test_as005_stable_bounded_pages(tmp_path):
    # AS-005 - Stable bounded pages
    store = ReviewAuditStore(tmp_path)
    for i in range(1, 6):
        record = ReviewAuditRecord(
            review_id=f"rev-p{i}",
            repository="test/repo",
            target_type="issue",
            target_number=str(200 + i),
            review_kind="issue_specification",
            origin="test",
            process_identity="proc-1",
            creation_time="2023-10-27T10:00:00Z",
            creation_sequence=i,
            reviewed_generation="gen-1",
            policy_identity="pol-1",
            related_issue_membership=None,
            diagnostic_execution_references=None,
            lifecycle=EvaluationLifecycle.FINISHED,
            execution_mode=ExecutionMode.EXECUTED,
            native_verdict=None,
            native_report=None,
            source_review_id=None,
        )
        assert store.record_evaluation(record)

    # Request first page limit 3
    page1_res = store.get_recent_history("test/repo", limit=3, high_water_mark_seq=5)
    page1 = page1_res.records
    assert len(page1) == 3
    assert page1[0].review_id == "rev-p5"
    assert page1[1].review_id == "rev-p4"
    assert page1[2].review_id == "rev-p3"

    # Insert new record
    record_new = ReviewAuditRecord(
        review_id="rev-p6",
        repository="test/repo",
        target_type="issue",
        target_number="206",
        review_kind="issue_specification",
        origin="test",
        process_identity="proc-1",
        creation_time="2023-10-27T10:00:00Z",
        creation_sequence=6,
        reviewed_generation="gen-1",
        policy_identity="pol-1",
        related_issue_membership=None,
        diagnostic_execution_references=None,
        lifecycle=EvaluationLifecycle.FINISHED,
        execution_mode=ExecutionMode.EXECUTED,
        native_verdict=None,
        native_report=None,
        source_review_id=None,
    )
    store.record_evaluation(record_new)

    # Next page using high water mark 5 should still only see up to 5, skipping 6
    # To get page 2, we would typically query WHERE creation_sequence < (lowest from page 1).
    # Wait, our query is `creation_sequence <= ?`. So it will return 5, 4, 3 if limit is 3.
    # The requirement is that new arrivals don't reorder or skip.
    page1_again_res = store.get_recent_history("test/repo", limit=3, high_water_mark_seq=5)
    page1_again = page1_again_res.records
    assert len(page1_again) == 3
    assert page1_again[0].review_id == "rev-p5"


def test_as006_audit_failure_and_redaction(tmp_path):
    # AS-006 - Audit failure and redaction

    # Test redaction logic explicitly
    data = {"some_token": "ghp_1234567890abcdefGH", "nested": {"AUTHORIZATION": "Bearer my_secret_token", "access-token": "abc1234", "normal_text": "Here is my github_pat_11ABCD1234"}, "list": ["sk-proj-xyz123"], "supplied_cred": "SUPER_SECRET_123", "near_miss": "ghx_123456"}

    redacted = redact_sensitive_data(data, credentials=["SUPER_SECRET_123"])

    assert redacted["some_token"] == "[REDACTED]"
    assert redacted["nested"]["AUTHORIZATION"] == "[REDACTED]"
    assert redacted["nested"]["access-token"] == "[REDACTED]"
    assert redacted["nested"]["normal_text"] == "Here is my [REDACTED]"
    assert redacted["list"][0] == "[REDACTED]"
    assert redacted["supplied_cred"] == "[REDACTED]"
    assert redacted["near_miss"] == "ghx_123456"

    # Test recording failure (unwritable root)
    unwritable = tmp_path / "unwritable"
    unwritable.mkdir(mode=0o444)

    store = ReviewAuditStore(unwritable)
    record = ReviewAuditRecord(
        review_id="rev-fail",
        repository="test/repo",
        target_type="issue",
        target_number="42",
        review_kind="issue_specification",
        origin="test",
        process_identity="proc-1",
        creation_time="2023-10-27T10:00:00Z",
        creation_sequence=1,
        reviewed_generation="gen-1",
        policy_identity="pol-1",
        related_issue_membership=None,
        diagnostic_execution_references=None,
        lifecycle=EvaluationLifecycle.FINISHED,
        execution_mode=ExecutionMode.EXECUTED,
        native_verdict=None,
        native_report=None,
        source_review_id=None,
    )
    # Shouldn't raise, should just return False
    assert store.record_evaluation(record) is False


from auto_coder.review_audit import ReviewAuditStore, StorageHealth


def test_uninit_read(tmp_path):
    store = ReviewAuditStore(tmp_path)
    res = store.get_evaluation("test/repo", "rev-1")
    assert res.health == StorageHealth.UNINITIALIZED

    # ensure it did not create the directory or DB file
    assert not (tmp_path / "test_repo").exists()
    assert not (tmp_path / "test_repo" / "audit.db").exists()
