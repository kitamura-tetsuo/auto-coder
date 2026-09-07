from unittest.mock import MagicMock, patch

from src.auto_coder.util.gh_cache import GitHubClient


def _response(status: int, payload: object) -> MagicMock:
    response = MagicMock(status_code=status)
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def test_dependency_boundary_paginates_and_retains_stable_ids(mock_github_token):
    first_page = [{"number": number, "id": 10_000 + number} for number in range(1, 101)]
    http = MagicMock()
    http.get.side_effect = [_response(200, first_page), _response(200, [{"number": 205, "id": 991_205}])]
    context = MagicMock()
    context.__enter__.return_value = http

    with patch("src.auto_coder.util.gh_cache.httpx.Client", return_value=context):
        relationships = GitHubClient.get_instance("token").get_blocked_by_strict("owner/repo", 101)

    assert len(relationships) == 101
    assert relationships[-1] == {"number": 205, "id": 991_205}
    assert http.get.call_args_list[0].kwargs["params"] == {"per_page": 100, "page": 1}
    assert http.get.call_args_list[1].kwargs["params"] == {"per_page": 100, "page": 2}


def test_dependency_mutation_uses_github_issue_id_not_issue_number(mock_github_token):
    http = MagicMock()
    http.post.return_value = _response(201, {})
    context = MagicMock()
    context.__enter__.return_value = http

    with patch("src.auto_coder.util.gh_cache.httpx.Client", return_value=context):
        GitHubClient.get_instance("token").mutate_blocked_by_strict("owner/repo", 101, 991_205, add=True)

    assert http.post.call_args.kwargs["json"] == {"issue_id": 991_205}
    assert http.post.call_args.args[0].endswith("/issues/101/dependencies/blocked_by")
