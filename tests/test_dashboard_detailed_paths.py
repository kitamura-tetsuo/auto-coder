import pytest

from auto_coder.dashboard import generate_activity_diagram


def test_pr_happy_path_merge():
    logs = [
        {"category": "PR Processing", "message": "Processing PR #1", "details": {"branch": "feature"}, "timestamp": 1000},
        {"category": "CI Status", "message": "Success", "details": {"success": True}, "timestamp": 1001},
        {"category": "Merge Check", "message": "Mergeable", "details": {"mergeable": True}, "timestamp": 1002},
        {"category": "Merging", "message": "Successfully merged", "details": {}, "timestamp": 1003},
        {"category": "Decision", "message": "Finished", "details": {}, "timestamp": 1004},
    ]

    diagram = generate_activity_diagram(logs, "pr")

    assert "class Start visited" in diagram
    assert "class CheckCI visited" in diagram
    assert "class CheckMerge visited" in diagram
    assert "class Merge visited" in diagram
    assert "class Cleanup visited" in diagram
    assert "class End visited" in diagram

    # Negative assertions
    assert "class Remediate visited" not in diagram
    assert "class FixIssues visited" not in diagram


def test_pr_ci_failure_fix_local():
    logs = [
        {"category": "PR Processing", "message": "Processing PR #2", "details": {"branch": "fix"}, "timestamp": 1000},
        {"category": "CI Status", "message": "Failure", "details": {"success": False, "in_progress": False}, "timestamp": 1001},
        # Implied CheckJules -> No
        {"category": "Update Base", "message": "Skipped", "details": {"result": "skipped"}, "timestamp": 1002},
        {"category": "Fixing Issues", "message": "Fixing", "details": {}, "timestamp": 1003},
        {"category": "Decision", "message": "Finished", "details": {}, "timestamp": 1004},
    ]

    diagram = generate_activity_diagram(logs, "pr")

    assert "class Start visited" in diagram
    assert "class CheckCI visited" in diagram
    assert "class CheckJules visited" in diagram
    assert "class UpdateBase visited" in diagram
    assert "class FixIssues visited" in diagram
    assert "class CommitFix visited" in diagram
    assert "class End visited" in diagram


def test_pr_remediation_success():
    logs = [
        {"category": "PR Processing", "message": "Processing PR #3", "details": {}, "timestamp": 1000},
        {"category": "CI Status", "message": "Success", "details": {"success": True}, "timestamp": 1001},
        {"category": "Merge Check", "message": "Not Mergeable", "details": {"mergeable": False}, "timestamp": 1002},
        {"category": "Remediation", "message": "Starting", "details": {"state": "dirty"}, "timestamp": 1003},
        {"category": "Remediation", "message": "Updating Base", "details": {"step": "update_base"}, "timestamp": 1004},
        {"category": "Remediation", "message": "Success", "details": {"result": "success"}, "timestamp": 1005},
    ]

    diagram = generate_activity_diagram(logs, "pr")

    assert "class Remediate visited" in diagram
    assert "class CheckConflict visited" in diagram
    assert "class PushUpdate visited" in diagram
    assert "class End visited" in diagram
    # Regular update base shouldn't be visited if remediation logic handled it separately in graph logic,
    # but my implementation separates them.
    # Remediate log is present, so "Remediate" node is visited.


def test_issue_jules_mode():
    logs = [
        {"category": "Issue Processing", "message": "Processing", "details": {}, "timestamp": 1000},
        {"category": "Dispatch", "message": "Dispatching", "details": {"mode": "jules"}, "timestamp": 1001},
        {"category": "Jules Session", "message": "Started", "details": {}, "timestamp": 1002},
    ]

    diagram = generate_activity_diagram(logs, "issue")

    assert "class Start visited" in diagram
    assert "class CheckLabel visited" in diagram
    assert "class CheckType visited" in diagram
    assert "class CheckMode visited" in diagram
    assert "class StartSession visited" in diagram
    assert "class Comment visited" in diagram
    assert "class End visited" in diagram

    assert "class BranchSetup visited" not in diagram


def test_issue_direct_mode():
    logs = [
        {"category": "Issue Processing", "message": "Processing", "details": {}, "timestamp": 1000},
        {"category": "Dispatch", "message": "Dispatching", "details": {"mode": "local"}, "timestamp": 1001},
        {"category": "Branch Setup", "message": "Setup", "details": {}, "timestamp": 1002},
        {"category": "Analysis Start", "message": "Analyzing", "details": {}, "timestamp": 1003},
        {"category": "Apply Changes", "message": "Applying", "details": {}, "timestamp": 1004},
        {"category": "Create PR", "message": "Created", "details": {}, "timestamp": 1005},
    ]

    diagram = generate_activity_diagram(logs, "issue")

    assert "class Start visited" in diagram
    assert "class CheckMode visited" in diagram
    assert "class BranchSetup visited" in diagram
    assert "class Analyze visited" in diagram
    assert "class Apply visited" in diagram
    assert "class CreatePR visited" in diagram
    assert "class End visited" in diagram
