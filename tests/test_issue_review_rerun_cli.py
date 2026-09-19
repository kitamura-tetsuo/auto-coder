import os
import platform
from dataclasses import dataclass

import pytest
from click.testing import CliRunner

from auto_coder.cli import main
from auto_coder.issue_review_rerun import SubjectRerunStatus
from auto_coder.issue_review_rerun_scope import IssueReviewRerunScopeResolver
from auto_coder.lock_manager import LockManager
from auto_coder.util.gh_cache import OpenGitHubEntities, OpenGitHubIssue

REPO = "owner/repo"


@dataclass
class FakeGitHub:
    snapshots: dict[int, dict[str, object]]
    parents: dict[int, int]
    children: dict[int, list[int]]

    def get_item_type_strict(self, _repo: str, number: int) -> str:
        return "pr" if "pull_request" in self.snapshots[number] else "issue"

    def get_issue_dispatch_snapshot_strict(self, _repo: str, number: int) -> dict[str, object]:
        return self.snapshots[number]

    def get_parent_issue_details_strict(self, _repo: str, number: int):
        parent = self.parents.get(number)
        return None if parent is None else self.snapshots[parent]

    def get_direct_sub_issues_strict(self, _repo: str, number: int) -> list[dict[str, object]]:
        return [self.snapshots[child] for child in self.children.get(number, [])]

    def get_open_entities_strict(self, _repo: str) -> OpenGitHubEntities:
        issues = [OpenGitHubIssue(number, None) for number, snapshot in self.snapshots.items() if snapshot.get("state") == "open" and "pull_request" not in snapshot]
        return OpenGitHubEntities(issues, [])


def issue(number: int, state: str = "open", body: str = "", ready: bool = True) -> dict[str, object]:
    return {"number": number, "state": state, "body": body, "title": str(number), "labels": [{"name": "implementation-ready"}] if ready else []}


def test_family_scope_retains_closed_child_and_has_no_parent_individual() -> None:
    github = FakeGitHub({10: issue(10), 11: issue(11, body="Parent Issue: #10"), 12: issue(12, "closed", "parent_issue: 10")}, {11: 10, 12: 10}, {10: [11, 12]})
    scope = IssueReviewRerunScopeResolver(github, REPO).family(10)
    assert [(item.kind, item.issue_number) for item in scope.subjects] == [("decomposition", 10), ("individual", 11), ("individual", 12)]


def test_issue_rejects_family_member_and_unmaterialized_declaration() -> None:
    child = FakeGitHub({10: issue(10), 11: issue(11, body="Parent-Issue: #10")}, {11: 10}, {10: [11]})
    with pytest.raises(ValueError, match="use --family"):
        IssueReviewRerunScopeResolver(child, REPO).issue(11)
    declared = FakeGitHub({11: issue(11, body="Parent-Issue: #10")}, {}, {})
    with pytest.raises(ValueError, match="not natively materialized"):
        IssueReviewRerunScopeResolver(declared, REPO).issue(11)


def test_all_deduplicates_family_and_excludes_open_child_of_closed_parent() -> None:
    github = FakeGitHub(
        {1: issue(1), 10: issue(10), 11: issue(11, body="Parent-Issue: 10"), 12: issue(12, "closed", "Parent-Issue: #10"), 20: issue(20, "closed"), 21: issue(21, body="Parent-Issue: 20"), 99: {**issue(99), "pull_request": {}}},
        {11: 10, 12: 10, 21: 20},
        {10: [11, 12], 20: [21]},
    )
    scope = IssueReviewRerunScopeResolver(github, REPO).all()
    assert [(item.kind, item.issue_number) for item in scope.subjects] == [("decomposition", 10), ("individual", 1), ("individual", 11), ("individual", 12)]
    assert scope.exclusions == ("Issue #21: native parent #20 is closed",)


def test_cli_requires_exactly_one_selector_without_contacting_github() -> None:
    result = CliRunner().invoke(main, ["review", "rerun", "--repo", REPO])
    assert result.exit_code == 2
    assert "exactly one" in result.output


def test_cli_dry_run_and_acceptance_use_resolved_exact_subjects(monkeypatch) -> None:
    github = FakeGitHub({1: issue(1, ready=False)}, {}, {})
    accepted: list[tuple[str, tuple[object, ...]]] = []

    class Engine:
        def __init__(self, supplied_github, config):
            self.github = supplied_github

        def _is_issue_specification_validation_enabled(self, _repo):
            return True

        def _is_issue_decomposition_validation_enabled(self, _repo):
            return True

        def accept_issue_review_rerun(self, request_id, subjects):
            accepted.append((request_id, tuple(subjects)))
            return tuple(SubjectRerunStatus(subject, request_id, 1, "deferred", "missing readiness") for subject in subjects)

    monkeypatch.setattr("auto_coder.cli_commands_review.GitHubClient.get_instance", lambda _token: github)
    monkeypatch.setattr("auto_coder.cli_commands_review.AutomationEngine", Engine)
    monkeypatch.setattr("auto_coder.cli_commands_review.get_github_token_or_fail", lambda _token: "token")
    monkeypatch.setattr("auto_coder.cli_commands_review.LockManager.is_locked", lambda _self: False)
    runner = CliRunner()
    dry = runner.invoke(main, ["review", "rerun", "--repo", REPO, "--issue", "1", "--dry-run"])
    assert dry.exit_code == 0
    assert '"mode": "dry-run"' in dry.output
    assert "controller is stopped" in dry.output
    assert accepted == []
    live = runner.invoke(main, ["review", "rerun", "--repo", REPO, "--issue", "1"])
    assert live.exit_code == 0
    assert '"mode": "accepted"' in live.output
    assert len(accepted) == 1
    assert [(subject.kind, subject.issue_number) for subject in accepted[0][1]] == [("individual", 1)]


def test_root_invocation_preserves_live_controller_lock_with_force(monkeypatch, tmp_path) -> None:
    github = FakeGitHub({7: issue(7)}, {}, {})
    accepted: list[tuple[str, tuple[object, ...]]] = []

    class Engine:
        def __init__(self, supplied_github, config):
            self.github = supplied_github

        def accept_issue_review_rerun(self, request_id, subjects):
            selected = tuple(subjects)
            accepted.append((request_id, selected))
            return tuple(SubjectRerunStatus(subject, request_id, 1, "pending") for subject in selected)

    monkeypatch.setattr("auto_coder.cli_commands_review.GitHubClient.get_instance", lambda _token: github)
    monkeypatch.setattr("auto_coder.cli_commands_review.AutomationEngine", Engine)
    monkeypatch.setattr("auto_coder.cli_commands_review.get_github_token_or_fail", lambda _token: "token")
    monkeypatch.setattr("auto_coder.cli_commands_review.get_issue_specification_validation_from_config", lambda **_kwargs: True)
    monkeypatch.setattr("auto_coder.cli_commands_review.get_issue_decomposition_validation_from_config", lambda **_kwargs: True)
    lock_path = tmp_path / "controller.lock"
    monkeypatch.setattr(LockManager, "_get_lock_file_path", lambda _self: lock_path)

    lock = LockManager()
    assert lock.lock_file_path == lock_path
    assert not lock.is_locked()
    assert lock.acquire_lock()
    try:
        before = lock.lock_file_path.read_bytes()
        held = lock.get_lock_info_obj()
        assert held is not None
        assert (held.hostname, held.pid) == (platform.node(), os.getpid())

        runner = CliRunner()
        dry = runner.invoke(main, ["review", "rerun", "--repo", REPO, "--issue", "7", "--dry-run"])
        assert dry.exit_code == 0, dry.output
        assert lock.lock_file_path.read_bytes() == before
        assert accepted == []

        live = runner.invoke(main, ["--force", "review", "rerun", "--repo", REPO, "--issue", "7"])
        assert live.exit_code == 0, live.output
        assert lock.lock_file_path.read_bytes() == before
        after = lock.get_lock_info_obj()
        assert after is not None
        assert (after.hostname, after.pid) == (held.hostname, held.pid)
        assert len(accepted) == 1
        assert [(subject.repository, subject.kind, subject.issue_number) for subject in accepted[0][1]] == [(REPO, "individual", 7)]
    finally:
        lock.release_lock()


@pytest.mark.parametrize("value", ["0", "-1", "+1", "1.0", "01", "abc"])
def test_cli_rejects_non_positive_decimal_selector(value: str) -> None:
    result = CliRunner().invoke(main, ["review", "rerun", "--repo", REPO, "--issue", value])
    assert result.exit_code == 2
    assert "positive decimal" in result.output
