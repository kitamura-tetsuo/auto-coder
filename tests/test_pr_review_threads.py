from unittest.mock import MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.github_app_reviewer import ReviewPublicationResult
from auto_coder.pr_processor import (
    _get_review_thread_gate_state,
    _handle_pr_merge,
    _merge_pr,
    _process_pr_for_merge,
    _take_pr_actions,
    has_unresolved_review_threads,
)
from auto_coder.util.gh_cache import GitHubClient, ReviewThread, ReviewThreadComment
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


def test_strict_review_thread_gate_preserves_real_client_lookup_error(mock_github_client):
    mock_github_client.graphql_query.side_effect = Exception("GraphQL Network Error")

    state = _get_review_thread_gate_state(mock_github_client, "owner/repo", 101)

    assert state.has_unresolved is False
    assert state.lookup_error == "GraphQL Network Error"


@pytest.mark.parametrize(
    "node",
    [
        {"isResolved": False},
        {"id": "t1"},
        {"id": "t1", "isResolved": None},
    ],
    ids=["missing-id", "missing-state", "non-boolean-state"],
)
def test_strict_review_thread_lookup_rejects_ambiguous_identity_or_state(mock_github_client, node):
    mock_github_client.graphql_query.return_value = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [node],
                    }
                }
            }
        }
    }

    with pytest.raises(RuntimeError, match="without a valid ID|without an explicit resolved state"):
        mock_github_client.get_pr_review_threads_strict("owner/repo", 101)


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


@patch("auto_coder.pr_processor._handle_pr_merge")
@patch("auto_coder.pr_processor.LabelManager")
@patch("auto_coder.pr_processor.GitHubClient.get_instance")
def test_process_pr_for_merge_blocks_on_unresolved_threads(mock_gh_instance, mock_label_manager, mock_handle_merge):
    mock_handle_merge.return_value = ["Skipping merge for PR #123 due to unresolved review threads"]
    context_mock = MagicMock()
    context_mock.__enter__.return_value = MagicMock()
    mock_label_manager.return_value = context_mock

    config = AutomationConfig()
    config.AUTO_MERGE = True
    config.CHECK_LABELS = True
    pr_data = {"number": 123, "head": {"ref": "feature-123"}, "labels": []}

    result = _process_pr_for_merge("owner/repo", pr_data, config)

    assert any("Skipping merge for PR #123 due to unresolved review threads" in a for a in result.actions_taken)
    mock_handle_merge.assert_called_once_with(mock_gh_instance.return_value, "owner/repo", pr_data, config, {}, result)


@patch("auto_coder.pr_processor._handle_pr_merge")
@patch("auto_coder.pr_processor.LabelManager")
@patch("auto_coder.pr_processor.GitHubClient.get_instance")
def test_process_pr_for_merge_proceeds_when_resolved(mock_gh_instance, mock_label_manager, mock_handle_merge):
    mock_handle_merge.return_value = ["Successfully merged PR #123"]
    context_mock = MagicMock()
    lm_instance = MagicMock()
    context_mock.__enter__.return_value = lm_instance
    mock_label_manager.return_value = context_mock

    config = AutomationConfig()
    config.AUTO_MERGE = True
    config.CHECK_LABELS = True
    pr_data = {"number": 123, "head": {"ref": "feature-123"}, "labels": []}

    result = _process_pr_for_merge("owner/repo", pr_data, config)

    assert any("Successfully merged PR #123" in a for a in result.actions_taken)
    mock_handle_merge.assert_called_once_with(mock_gh_instance.return_value, "owner/repo", pr_data, config, {}, result)
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
    pr_data = {"number": 123, "head": {"ref": "feature-123"}, "labels": []}

    client = MagicMock()
    client.get_pr_review_threads_strict.return_value = []
    actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

    assert any("All GitHub Actions checks passed for PR #123" in a for a in actions)
    assert any("Skipping merge for PR #123 due to unresolved review threads" in a for a in actions)
    mock_merge_pr.assert_not_called()


@patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
@patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
@patch("auto_coder.pr_processor._check_github_actions_status")
@patch("auto_coder.pr_processor.run_adversarial_validation")
@patch("auto_coder.pr_processor._merge_pr")
def test_handle_pr_merge_fails_closed_when_real_review_thread_lookup_fails(mock_merge_pr, mock_validation, mock_checks, mock_mergeable, mock_exit_if_in_progress, mock_github_client):
    mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
    mock_github_client.graphql_query.side_effect = Exception("reviewThreads API unavailable")
    config = AutomationConfig()
    config.AUTO_MERGE = True
    pr_data = {"number": 123, "body": "Fixes #99", "labels": [], "head": {"ref": "feature-99", "sha": "current-head"}}

    actions = _handle_pr_merge(mock_github_client, "owner/repo", pr_data, config, {})

    # The stale-review-thread GitHub-marker scan (which reuses the same
    # review-thread lookup) now fails closed even earlier than the ordinary
    # unresolved-thread gate, for the same underlying lookup failure.
    assert any("registry could not be read" in action or "review threads could not be checked" in action for action in actions)
    mock_validation.assert_not_called()
    mock_merge_pr.assert_not_called()


@patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
@patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
@patch("auto_coder.pr_processor._check_github_actions_status")
@patch("auto_coder.pr_processor.has_unresolved_review_threads")
@patch("auto_coder.pr_processor.run_adversarial_validation")
@patch("auto_coder.pr_processor.publish_adversarial_review", return_value=ReviewPublicationResult(True, "APPROVE", ""))
@patch("auto_coder.pr_processor.isolated_pr_head_worktree")
@patch("auto_coder.pr_processor._merge_pr")
def test_handle_pr_merge_proceeds_when_all_threads_resolved(mock_merge_pr, mock_worktree, mock_publish, mock_adv_val, mock_has_unresolved, mock_checks, mock_mergeable, mock_exit_if_in_progress):
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
    client.get_pr_review_threads_strict.return_value = []
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


def test_get_pr_review_threads_includes_comments(mock_github_client):
    mock_response = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {
                                "id": "t1",
                                "isResolved": False,
                                "isOutdated": False,
                                "comments": {
                                    "nodes": [
                                        {"databaseId": 100, "body": "Original finding", "author": {"login": "chatgpt-codex-connector", "databaseId": 199175422}},
                                        {"databaseId": 101, "body": "Fixed it", "author": {"login": "agent[bot]"}},
                                    ]
                                },
                            }
                        ],
                    }
                }
            }
        }
    }
    mock_github_client.graphql_query.return_value = mock_response

    threads = mock_github_client.get_pr_review_threads("owner/repo", 101)
    assert len(threads) == 1
    assert len(threads[0].comments) == 2
    assert threads[0].comments[0] == ReviewThreadComment(database_id=100, body="Original finding", author_login="chatgpt-codex-connector", author_id=199175422)
    assert threads[0].comments[1].author_login == "agent[bot]"
    assert threads[0].comments_truncated is False


def test_get_pr_review_threads_marks_truncated_comment_list(mock_github_client):
    mock_response = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {
                                "id": "t1",
                                "isResolved": False,
                                "isOutdated": False,
                                "comments": {
                                    "pageInfo": {"hasNextPage": True},
                                    "nodes": [
                                        {"databaseId": 100, "body": "Original finding", "author": {"login": "chatgpt-codex-connector[bot]"}},
                                    ],
                                },
                            }
                        ],
                    }
                }
            }
        }
    }
    mock_github_client.graphql_query.return_value = mock_response

    threads = mock_github_client.get_pr_review_threads("owner/repo", 101)
    assert threads[0].comments_truncated is True


def test_get_pr_review_threads_missing_comments_field_defaults_empty(mock_github_client):
    mock_response = {
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
    mock_github_client.graphql_query.return_value = mock_response

    threads = mock_github_client.get_pr_review_threads("owner/repo", 101)
    assert threads[0].comments == []


def test_resolve_review_thread_success(mock_github_client):
    mock_github_client.graphql_query.return_value = {"data": {"resolveReviewThread": {"thread": {"id": "t1", "isResolved": True}}}}
    mock_github_client.resolve_review_thread("t1")  # should not raise
    mock_github_client.graphql_query.assert_called_once()


def test_resolve_review_thread_not_confirmed_raises(mock_github_client):
    mock_github_client.graphql_query.return_value = {"data": {"resolveReviewThread": {"thread": {"id": "t1", "isResolved": False}}}}
    with pytest.raises(RuntimeError):
        mock_github_client.resolve_review_thread("t1")


def test_resolve_review_thread_mutation_error_propagates(mock_github_client):
    mock_github_client.graphql_query.side_effect = Exception("GraphQL mutation failed")
    with pytest.raises(Exception):
        mock_github_client.resolve_review_thread("t1")


def test_reply_to_review_thread_uses_ghapi(mock_github_client):
    with patch("auto_coder.util.gh_cache.get_ghapi_client") as mock_get_ghapi:
        mock_api = MagicMock()
        mock_get_ghapi.return_value = mock_api
        mock_github_client.reply_to_review_thread("owner/repo", 5, 100, "explanation body")
        mock_api.pulls.create_reply_for_review_comment.assert_called_once_with("owner", "repo", 5, 100, body="explanation body")


def test_reply_to_review_thread_failure_propagates(mock_github_client):
    with patch("auto_coder.util.gh_cache.get_ghapi_client") as mock_get_ghapi:
        mock_api = MagicMock()
        mock_api.pulls.create_reply_for_review_comment.side_effect = Exception("API error")
        mock_get_ghapi.return_value = mock_api
        with pytest.raises(Exception):
            mock_github_client.reply_to_review_thread("owner/repo", 5, 100, "explanation body")


def test_unresolve_review_thread_success(mock_github_client):
    mock_github_client.graphql_query.return_value = {"data": {"unresolveReviewThread": {"thread": {"id": "t1", "isResolved": False}}}}
    mock_github_client.unresolve_review_thread("t1")  # should not raise


def test_unresolve_review_thread_not_confirmed_raises(mock_github_client):
    mock_github_client.graphql_query.return_value = {"data": {"unresolveReviewThread": {"thread": {"id": "t1", "isResolved": True}}}}
    with pytest.raises(RuntimeError):
        mock_github_client.unresolve_review_thread("t1")


def test_unresolve_review_thread_empty_payload_fails_closed(mock_github_client):
    """An empty/missing thread payload must not be mistaken for confirmed
    success just because `.get("isResolved")` on it is falsy."""
    mock_github_client.graphql_query.return_value = {"data": {"unresolveReviewThread": {"thread": {}}}}
    with pytest.raises(RuntimeError):
        mock_github_client.unresolve_review_thread("t1")


def test_unresolve_review_thread_missing_thread_key_fails_closed(mock_github_client):
    mock_github_client.graphql_query.return_value = {"data": {"unresolveReviewThread": {}}}
    with pytest.raises(RuntimeError):
        mock_github_client.unresolve_review_thread("t1")


def test_unresolve_review_thread_wrong_id_fails_closed(mock_github_client):
    mock_github_client.graphql_query.return_value = {"data": {"unresolveReviewThread": {"thread": {"id": "other-thread", "isResolved": False}}}}
    with pytest.raises(RuntimeError):
        mock_github_client.unresolve_review_thread("t1")


def test_unresolve_review_thread_mutation_error_propagates(mock_github_client):
    mock_github_client.graphql_query.side_effect = Exception("GraphQL mutation failed")
    with pytest.raises(Exception):
        mock_github_client.unresolve_review_thread("t1")


class TestClaimedReviewThreadGateState:
    def test_no_unresolved_threads_short_circuits(self):
        from auto_coder.pr_processor import _get_claimed_review_thread_state

        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []
        client.has_unresolved_review_threads.return_value = False

        with patch("auto_coder.pr_processor._get_review_thread_gate_state") as mock_gate:
            from auto_coder.pr_processor import ReviewThreadGateState

            mock_gate.return_value = ReviewThreadGateState(has_unresolved=False)
            state = _get_claimed_review_thread_state(client, "owner/repo", 101)

        assert state.claimed == ()
        assert state.has_blocking_unresolved is False
        assert state.lookup_error is None
        client.get_pr_review_threads_strict.assert_not_called()

    def test_lookup_error_propagates(self):
        from auto_coder.pr_processor import ReviewThreadGateState, _get_claimed_review_thread_state

        client = MagicMock()
        with patch("auto_coder.pr_processor._get_review_thread_gate_state") as mock_gate:
            mock_gate.return_value = ReviewThreadGateState(lookup_error="boom")
            state = _get_claimed_review_thread_state(client, "owner/repo", 101)

        assert state.lookup_error == "boom"
        assert state.claimed == ()

    def test_detailed_lookup_error_propagates(self):
        from auto_coder.pr_processor import ReviewThreadGateState, _get_claimed_review_thread_state

        client = GitHubClient(token="fake_token")
        client.get_pr_review_threads_strict = MagicMock(side_effect=RuntimeError("detailed GraphQL lookup failed"))

        with patch("auto_coder.pr_processor._get_review_thread_gate_state", return_value=ReviewThreadGateState(has_unresolved=True)):
            state = _get_claimed_review_thread_state(client, "owner/repo", 101)

        assert state.lookup_error == "detailed GraphQL lookup failed"
        assert state.has_blocking_unresolved is False
        assert state.claimed == ()

    def test_claimed_thread_does_not_block(self):
        from auto_coder.pr_processor import ReviewThreadGateState, _get_claimed_review_thread_state

        client = GitHubClient(token="fake_token")
        client.graphql_query = MagicMock(
            return_value={
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [
                                    {
                                        "id": "t1",
                                        "isResolved": False,
                                        "isOutdated": False,
                                        "comments": {
                                            "nodes": [
                                                {"databaseId": 1, "body": "finding", "author": {"login": "chatgpt-codex-connector", "databaseId": 199175422}},
                                                {"databaseId": 2, "body": "Fixed.\n<!-- auto-coder-review-addressed:v1 -->", "author": {"login": "agent[bot]"}},
                                            ]
                                        },
                                    }
                                ],
                            }
                        }
                    }
                }
            }
        )

        with patch("auto_coder.pr_processor._get_review_thread_gate_state") as mock_gate, patch("auto_coder.pr_processor._resolve_eligible_review_thread_ids", return_value={199175422}):
            mock_gate.return_value = ReviewThreadGateState(has_unresolved=True)
            state = _get_claimed_review_thread_state(client, "owner/repo", 101)

        assert state.lookup_error is None
        assert state.has_blocking_unresolved is False
        assert len(state.claimed) == 1
        assert state.claimed[0].thread_id == "t1"

    def test_blocking_thread_still_blocks_alongside_claimed(self):
        from auto_coder.pr_processor import ReviewThreadGateState, _get_claimed_review_thread_state

        client = GitHubClient(token="fake_token")
        client.graphql_query = MagicMock(
            return_value={
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [
                                    {
                                        "id": "t1",
                                        "isResolved": False,
                                        "comments": {
                                            "nodes": [{"databaseId": 1, "body": "finding", "author": {"login": "chatgpt-codex-connector", "databaseId": 199175422}}, {"databaseId": 2, "body": "Fixed.\n<!-- auto-coder-review-addressed:v1 -->", "author": {"login": "agent[bot]", "databaseId": 999}}]
                                        },
                                    },
                                    {
                                        "id": "t2",
                                        "isResolved": False,
                                        "comments": {"nodes": [{"databaseId": 3, "body": "please rename this", "author": {"login": "a-human"}}]},
                                    },
                                ],
                            }
                        }
                    }
                }
            }
        )

        with patch("auto_coder.pr_processor._get_review_thread_gate_state") as mock_gate, patch("auto_coder.pr_processor._resolve_eligible_review_thread_ids", return_value={199175422}):
            mock_gate.return_value = ReviewThreadGateState(has_unresolved=True)
            state = _get_claimed_review_thread_state(client, "owner/repo", 101)

        assert state.has_blocking_unresolved is True
        assert len(state.claimed) == 1
        assert tuple(thread.id for thread in state.unresolved) == ("t1", "t2")
        assert tuple(thread.id for thread in state.blocking_unresolved) == ("t2",)

    def test_no_strict_lookup_capability_fails_closed_to_blocking(self):
        from auto_coder.pr_processor import ReviewThreadGateState, _get_claimed_review_thread_state

        client = MagicMock()  # plain MagicMock has no class-level get_pr_review_threads_strict
        with patch("auto_coder.pr_processor._get_review_thread_gate_state") as mock_gate:
            mock_gate.return_value = ReviewThreadGateState(has_unresolved=True)
            state = _get_claimed_review_thread_state(client, "owner/repo", 101)

        assert state.has_blocking_unresolved is True
        assert state.claimed == ()
