from unittest.mock import MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.pr_processor import (
    _handle_pr_merge,
    _merge_pr,
    _process_pr_for_merge,
    _take_pr_actions,
    has_unresolved_review_threads,
)
from auto_coder.util.gh_cache import GitHubClient, ReviewThread
from auto_coder.util.github_action import GitHubActionsStatusResult


@pytest.fixture
def mock_github_client():
    client = GitHubClient(token="fake_token")
    client.graphql_query = MagicMock()
    return client


def test_review_thread_dataclass():
    thread = ReviewThread(id="thread_1", is_resolved=False, is_outdated=True)
    assert thread.id == "thread_1"
    assert thread.is_resolved is False
    assert thread.is_outdated is True

    default_thread = ReviewThread()
    assert default_thread.id == ""
    assert default_thread.is_resolved is False
    assert default_thread.is_outdated is False


def test_get_pr_review_threads_empty(mock_github_client):
    mock_response = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [],
                    }
                }
            }
        }
    }
    mock_github_client.graphql_query.return_value = mock_response

    threads = mock_github_client.get_pr_review_threads("owner/repo", 101)
    assert threads == []
    assert mock_github_client.has_unresolved_review_threads("owner/repo", 101) is False


def test_get_pr_review_threads_all_resolved(mock_github_client):
    mock_response = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {"id": "t1", "isResolved": True, "isOutdated": False},
                            {"id": "t2", "isResolved": True, "isOutdated": True},
                        ],
                    }
                }
            }
        }
    }
    mock_github_client.graphql_query.return_value = mock_response

    threads = mock_github_client.get_pr_review_threads("owner/repo", 101)
    assert len(threads) == 2
    assert threads[0].id == "t1"
    assert threads[0].is_resolved is True
    assert threads[1].id == "t2"
    assert threads[1].is_resolved is True
    assert mock_github_client.has_unresolved_review_threads("owner/repo", 101) is False


def test_get_pr_review_threads_single_unresolved(mock_github_client):
    mock_response = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {"id": "t1", "isResolved": False, "isOutdated": False},
                        ],
                    }
                }
            }
        }
    }
    mock_github_client.graphql_query.return_value = mock_response

    threads = mock_github_client.get_pr_review_threads("owner/repo", 101)
    assert len(threads) == 1
    assert threads[0].is_resolved is False
    assert mock_github_client.has_unresolved_review_threads("owner/repo", 101) is True


def test_get_pr_review_threads_mixed(mock_github_client):
    mock_response = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {"id": "t1", "isResolved": True, "isOutdated": False},
                            {"id": "t2", "isResolved": False, "isOutdated": True},
                            {"id": "t3", "isResolved": True, "isOutdated": False},
                        ],
                    }
                }
            }
        }
    }
    mock_github_client.graphql_query.return_value = mock_response

    threads = mock_github_client.get_pr_review_threads("owner/repo", 101)
    assert len(threads) == 3
    assert mock_github_client.has_unresolved_review_threads("owner/repo", 101) is True


def test_get_pr_review_threads_pagination(mock_github_client):
    page1 = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": True, "endCursor": "cursor_page_1"},
                        "nodes": [{"id": "t1", "isResolved": True}],
                    }
                }
            }
        }
    }
    page2 = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False, "endCursor": "cursor_page_2"},
                        "nodes": [{"id": "t2", "isResolved": False}],
                    }
                }
            }
        }
    }
    mock_github_client.graphql_query.side_effect = [page1, page2, page1, page2]

    threads = mock_github_client.get_pr_review_threads("owner/repo", 101)
    assert len(threads) == 2
    assert threads[0].id == "t1"
    assert threads[1].id == "t2"
    assert mock_github_client.graphql_query.call_count == 2
    assert mock_github_client.has_unresolved_review_threads("owner/repo", 101) is True


def test_get_pr_review_threads_api_error(mock_github_client):
    mock_github_client.graphql_query.side_effect = Exception("GraphQL Network Error")

    threads = mock_github_client.get_pr_review_threads("owner/repo", 101)
    assert threads == []
    assert mock_github_client.has_unresolved_review_threads("owner/repo", 101) is False


def test_has_unresolved_review_threads_helper(mock_github_client):
    mock_github_client.graphql_query.return_value = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [{"id": "t1", "isResolved": False}],
                    }
                }
            }
        }
    }
    assert has_unresolved_review_threads(mock_github_client, "owner/repo", 101) is True

    # Test with None client (falls back to singleton)
    with patch("auto_coder.pr_processor.GitHubClient.get_instance", return_value=mock_github_client):
        assert has_unresolved_review_threads(None, "owner/repo", 101) is True


@patch("auto_coder.pr_processor.has_unresolved_review_threads")
@patch("auto_coder.pr_processor._merge_pr")
@patch("auto_coder.pr_processor.LabelManager")
@patch("auto_coder.pr_processor.GitHubClient.get_instance")
def test_process_pr_for_merge_blocks_on_unresolved_threads(mock_gh_instance, mock_label_manager, mock_merge_pr, mock_has_unresolved):
    mock_has_unresolved.return_value = True
    context_mock = MagicMock()
    context_mock.__enter__.return_value = MagicMock()
    mock_label_manager.return_value = context_mock

    config = AutomationConfig()
    config.AUTO_MERGE = True
    config.CHECK_LABELS = True
    pr_data = {"number": 123, "labels": []}

    result = _process_pr_for_merge("owner/repo", pr_data, config)

    assert any("Skipping merge for PR #123 due to unresolved review threads" in a for a in result.actions_taken)
    mock_merge_pr.assert_not_called()


@patch("auto_coder.pr_processor.has_unresolved_review_threads")
@patch("auto_coder.pr_processor._merge_pr")
@patch("auto_coder.pr_processor.LabelManager")
@patch("auto_coder.pr_processor.GitHubClient.get_instance")
def test_process_pr_for_merge_proceeds_when_resolved(mock_gh_instance, mock_label_manager, mock_merge_pr, mock_has_unresolved):
    mock_has_unresolved.return_value = False
    mock_merge_pr.return_value = True
    context_mock = MagicMock()
    lm_instance = MagicMock()
    context_mock.__enter__.return_value = lm_instance
    mock_label_manager.return_value = context_mock

    config = AutomationConfig()
    config.AUTO_MERGE = True
    config.CHECK_LABELS = True
    pr_data = {"number": 123, "labels": []}

    result = _process_pr_for_merge("owner/repo", pr_data, config)

    assert any("Successfully merged PR #123" in a for a in result.actions_taken)
    mock_merge_pr.assert_called_once()
    lm_instance.keep_label.assert_called_once()


@patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
@patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
@patch("auto_coder.pr_processor._check_github_actions_status")
@patch("auto_coder.pr_processor.has_unresolved_review_threads")
@patch("auto_coder.pr_processor._merge_pr")
def test_handle_pr_merge_blocks_on_unresolved_threads(mock_merge_pr, mock_has_unresolved, mock_checks, mock_mergeable, mock_exit_if_in_progress):
    mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
    mock_has_unresolved.return_value = True

    config = AutomationConfig()
    config.AUTO_MERGE = True
    pr_data = {"number": 123, "labels": []}

    client = MagicMock()
    actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

    assert any("All GitHub Actions checks passed for PR #123" in a for a in actions)
    assert any("Skipping merge for PR #123 due to unresolved review threads" in a for a in actions)
    mock_merge_pr.assert_not_called()


@patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
@patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
@patch("auto_coder.pr_processor._check_github_actions_status")
@patch("auto_coder.pr_processor.has_unresolved_review_threads")
@patch("auto_coder.pr_processor.run_adversarial_validation")
@patch("auto_coder.pr_processor.isolated_pr_head_worktree")
@patch("auto_coder.pr_processor._merge_pr")
def test_handle_pr_merge_proceeds_when_all_threads_resolved(mock_merge_pr, mock_worktree, mock_adv_val, mock_has_unresolved, mock_checks, mock_mergeable, mock_exit_if_in_progress):
    mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
    mock_worktree.return_value.__enter__.return_value = "/tmp/worktree"
    mock_has_unresolved.return_value = False
    mock_merge_pr.return_value = True
    from auto_coder.adversarial_validator import AdversarialValidationResult

    mock_adv_val.return_value = AdversarialValidationResult(result="PASS", summary="Pass", findings=[])

    config = AutomationConfig()
    config.AUTO_MERGE = True
    pr_data = {"number": 123, "body": "Fixes #99", "labels": [], "head": {"ref": "feature-123", "sha": "123abc456"}}

    client = MagicMock()
    client.get_pull_request.return_value = {"head": {"sha": "123abc456"}}
    actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

    assert any("Successfully merged PR #123" in a for a in actions)
    mock_adv_val.assert_called_once()
    mock_merge_pr.assert_called_once()


@patch("auto_coder.pr_processor.has_unresolved_review_threads")
@patch("auto_coder.util.gh_cache.get_ghapi_client")
def test_merge_pr_safeguard_blocks_unresolved_threads(mock_get_ghapi, mock_has_unresolved):
    mock_has_unresolved.return_value = True

    client = MagicMock()
    config = AutomationConfig()
    config.MERGE_METHOD = "--squash"

    merged = _merge_pr("owner/repo", 123, {}, config, github_client=client)

    assert merged is False
    mock_get_ghapi.assert_not_called()


@patch("auto_coder.pr_processor._handle_pr_merge")
def test_take_pr_actions_defers_on_unresolved_threads(mock_handle_merge):
    mock_handle_merge.return_value = [
        "All GitHub Actions checks passed for PR #123",
        "Skipping merge for PR #123 due to unresolved review threads",
    ]

    client = MagicMock()
    config = AutomationConfig()
    pr_data = {"number": 123}

    actions = _take_pr_actions(client, "owner/repo", pr_data, config)

    assert any("Skipping merge for PR #123 due to unresolved review threads" in a for a in actions)
    assert any("PR #123 processing deferred." in a for a in actions)
