"""Regression coverage for explicit-local unresolved-review routing."""

from unittest.mock import MagicMock, patch

from auto_coder.codex_pr_attribution import AttributionDisposition, AttributionResult
from auto_coder.pr_processor import (
    ReviewRepairRouteDisposition,
    _delegate_cloud_review_thread_repair,
    _select_review_repair_route,
)
from auto_coder.util.gh_cache import PullRequestRoutingMetadata


def _pr(body: str = "<!-- auto-coder:local-llm -->\nCloses #7") -> dict:
    return {
        "number": 42,
        "body": body,
        "head": {"ref": "stale", "sha": "stale"},
        "base": {"ref": "main"},
        "user": {"login": "maintainer"},
    }


def _metadata(body: str = "<!-- auto-coder:local-llm -->\nCloses #7") -> PullRequestRoutingMetadata:
    return PullRequestRoutingMetadata(
        api_origin="https://api.github.com",
        repository="owner/repo",
        number=42,
        state="open",
        body=body,
        head_repository="owner/repo",
        head_ref="issue-7_attempt-1",
        head_sha="live-sha",
    )


def _client(*metadata: PullRequestRoutingMetadata) -> MagicMock:
    client = MagicMock()
    client.get_pull_request_routing_metadata_strict = MagicMock(side_effect=metadata)
    return client


@patch("auto_coder.pr_processor.resolve_codex_pr_origin", return_value=AttributionResult(AttributionDisposition.UNRESOLVED))
@patch("auto_coder.pr_processor.CloudManager.get_binding", return_value=None)
def test_explicit_local_route_ignores_linked_issue_resolution(_binding, _attribution) -> None:
    client = _client(_metadata())

    decision = _select_review_repair_route("owner/repo", _pr(), client)

    assert decision.disposition is ReviewRepairRouteDisposition.LOCAL_REQUIRED
    assert decision.evidence == _metadata()
    assert "owner/repo PR #42" in decision.reason


@patch("auto_coder.pr_processor.resolve_codex_pr_origin", return_value=AttributionResult(AttributionDisposition.UNRESOLVED))
@patch("auto_coder.pr_processor.CloudManager.get_binding", return_value=None)
def test_local_route_stops_before_cloud_delivery_and_reports_not_executed(_binding, _attribution) -> None:
    client = _client(_metadata(), _metadata())

    with patch("auto_coder.pr_processor._resolve_cloud_task_origin") as cloud_origin:
        result = _delegate_cloud_review_thread_repair("owner/repo", _pr(), client)

    assert result.route_disposition == "LOCAL_REQUIRED"
    assert result.delivered is False
    assert result == ["LOCAL_REQUIRED for PR #42: explicit local review repair is required for https://api.github.com owner/repo PR #42 at owner/repo:issue-7_attempt-1@live-sha; local review repair has not been executed"]
    cloud_origin.assert_not_called()


@patch("auto_coder.pr_processor.resolve_codex_pr_origin", return_value=AttributionResult(AttributionDisposition.UNRESOLVED))
@patch("auto_coder.pr_processor.CloudManager.get_binding", return_value=None)
def test_changed_authoritative_marker_invalidates_selected_local_route(_binding, _attribution) -> None:
    client = _client(_metadata(), _metadata("Closes #7"))

    with patch("auto_coder.pr_processor._resolve_cloud_task_origin") as cloud_origin:
        result = _delegate_cloud_review_thread_repair("owner/repo", _pr(), client)

    assert result.route_disposition == "CONFLICT"
    assert result.delivered is False
    assert "routing CONFLICT" in result[0]
    cloud_origin.assert_not_called()


@patch("auto_coder.pr_processor.resolve_codex_pr_origin", return_value=AttributionResult(AttributionDisposition.UNRESOLVED))
def test_exact_pr_binding_conflicts_with_explicit_local_marker(_attribution) -> None:
    client = _client(_metadata())
    with patch("auto_coder.pr_processor.CloudManager.get_binding", return_value=MagicMock(provider="jules", task_id="task")):
        decision = _select_review_repair_route("owner/repo", _pr(), client)

    assert decision.disposition is ReviewRepairRouteDisposition.CONFLICT
    assert "exact-PR cloud association" in decision.reason


def test_failed_authoritative_read_is_unavailable_not_cloud_fallback() -> None:
    client = MagicMock()
    client.get_pull_request_routing_metadata_strict = MagicMock(side_effect=RuntimeError("offline"))

    decision = _select_review_repair_route("owner/repo", _pr(), client)

    assert decision.disposition is ReviewRepairRouteDisposition.UNAVAILABLE
    assert "offline" in decision.reason
