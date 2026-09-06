from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig, Candidate
from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.decomposition_analyzer import DecompositionAnalysisResult
from src.auto_coder.decomposition_validation_lifecycle import DecompositionValidationLifecycle
from src.auto_coder.parent_issue_reconciliation import ParentDeclarationStatus, parse_parent_declaration
from src.auto_coder.specification_analyzer import SpecificationAnalysisResult
from src.auto_coder.specification_validation_lifecycle import SpecificationValidationLifecycle
from src.auto_coder.util.gh_cache import GitHubClient


@pytest.mark.parametrize("key", ["Parent-Issue", "parent_issue", "PARENT ISSUE"])
def test_parser_accepts_all_supported_keys_and_equivalent_repetitions(key: str):
    declaration = parse_parent_declaration(f" {key}: #17 \nparent-issue: 17")
    assert declaration.status is ParentDeclarationStatus.SUPPORTED
    assert declaration.parent_number == 17


@pytest.mark.parametrize(
    "body",
    [
        "Parent-Issue: #abc",
        "Parent-Issue: 0",
        "Parent-Issue: #1 trailing",
        "Parent-Issue: #1\nparent issue: #2",
    ],
)
def test_parser_rejects_every_malformed_or_ambiguous_candidate(body: str):
    declaration = parse_parent_declaration(body)
    assert declaration.status is ParentDeclarationStatus.INVALID
    assert declaration.parent_number is None


def test_webhook_child_reconciles_and_refetches_before_eager_validation(tmp_path: Path):
    """The supported invalidation origin preserves reconciliation ordering."""
    body = "## Requirements\n- REQ-001: Keep the graph authoritative."
    initial_child = {"id": 202, "number": 2, "title": "Child", "body": body + "\nParent-Issue: #1", "state": "open", "labels": []}
    refreshed_child = dict(initial_child, parent_issue_url="https://api.github.com/repos/o/r/issues/1")
    parent = {"id": 101, "number": 1, "title": "Parent", "body": body, "state": "open", "labels": [{"name": "implementation-ready"}]}
    GitHubClient.reset_singleton()
    github = GitHubClient.get_instance("token")
    github.get_parent_issue_details_strict = MagicMock()
    github.get_issue_dispatch_snapshot_strict = MagicMock()
    github.get_direct_sub_issues_strict = MagicMock()
    github.add_sub_issue_strict = MagicMock()
    github.get_parent_issue_details_strict.side_effect = [None, None, dict(parent)]
    github.get_issue_dispatch_snapshot_strict.side_effect = [dict(parent), dict(refreshed_child), dict(parent), dict(refreshed_child)]
    github.get_direct_sub_issues_strict.return_value = [dict(refreshed_child)]
    events: list[str] = []
    github.add_sub_issue_strict.side_effect = lambda *_args: events.append("linked")

    engine = AutomationEngine(github, AutomationConfig())
    engine._decomposition_validators["o/r"] = DecompositionValidationLifecycle("o/r", "provider/model", tmp_path / "sets.json", lambda *_args: events.append("decomposition") or DecompositionAnalysisResult("READY"))
    engine._specification_validators["o/r"] = SpecificationValidationLifecycle("o/r", "provider/model", tmp_path / "issues.json", lambda *_args: events.append("individual") or SpecificationAnalysisResult("READY"))

    engine._validate_submitted_parent_generation_for_child("o/r", 2, initial_child)

    assert events == ["linked", "decomposition", "individual"]
    github.add_sub_issue_strict.assert_called_once_with("o/r", 1, 2, 202)


def test_invalid_marker_blocks_common_dispatch_without_side_effects():
    issue = {"id": 202, "number": 2, "title": "Child", "body": "Parent-Issue: #abc", "state": "open", "labels": [{"name": "implementation-ready"}], "user": {"id": 1}}
    GitHubClient.reset_singleton()
    github = GitHubClient.get_instance("token")
    github.get_parent_issue_details_strict = MagicMock()
    github.add_sub_issue_strict = MagicMock()
    github.remove_labels = MagicMock()
    github.add_comment_to_issue = MagicMock()
    github.get_parent_issue_details_strict.return_value = None
    engine = AutomationEngine(github, AutomationConfig())
    engine._is_issue_author_allowed = MagicMock(return_value=True)

    with patch.object(engine, "_process_single_candidate_reserved") as implementation:
        result = engine._process_single_candidate_unified("o/r", Candidate(type="issue", data=issue, priority=0), engine.config)

    assert result.error == "Parent-Issue reconciliation blocked processing: malformed Parent-Issue declaration"
    assert result.actions == ["Blocked - invalid Parent-Issue relationship metadata"]
    implementation.assert_not_called()
    github.add_sub_issue_strict.assert_not_called()
    github.remove_labels.assert_not_called()
    github.add_comment_to_issue.assert_not_called()
