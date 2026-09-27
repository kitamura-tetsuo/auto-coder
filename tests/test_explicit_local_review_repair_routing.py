"""Regression coverage for explicit-local unresolved-review routing."""

import builtins
import json
from dataclasses import replace
from unittest.mock import MagicMock, patch

from auto_coder.cloud_manager import CloudManager, CloudTaskBinding
from auto_coder.cloud_run import CloudRunRepository
from auto_coder.codex_pr_attribution import AttributionDisposition, AttributionResult, CodexPrAttributionRepository
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
@patch("auto_coder.pr_processor.CloudManager.read_bindings_strict", return_value={})
def test_explicit_local_route_ignores_linked_issue_resolution(_binding, _attribution) -> None:
    client = _client(_metadata())

    decision = _select_review_repair_route("owner/repo", _pr(), client)

    assert decision.disposition is ReviewRepairRouteDisposition.LOCAL_REQUIRED
    assert decision.evidence == _metadata()
    assert "owner/repo PR #42" in decision.reason


@patch("auto_coder.pr_processor.resolve_codex_pr_origin", return_value=AttributionResult(AttributionDisposition.UNRESOLVED))
@patch("auto_coder.pr_processor.CloudManager.read_bindings_strict", return_value={})
def test_local_route_stops_before_cloud_delivery_and_reports_not_executed(_binding, _attribution) -> None:
    client = _client(_metadata(), _metadata())

    with patch("auto_coder.pr_processor._resolve_cloud_task_origin") as cloud_origin:
        result = _delegate_cloud_review_thread_repair("owner/repo", _pr(), client)

    assert result.route_disposition == "LOCAL_REQUIRED"
    assert result.delivered is False
    assert result == ["LOCAL_REQUIRED for PR #42: explicit local review repair is required for https://api.github.com owner/repo PR #42 at owner/repo:issue-7_attempt-1@live-sha; local review repair has not been executed"]
    cloud_origin.assert_not_called()


@patch("auto_coder.pr_processor.resolve_codex_pr_origin", return_value=AttributionResult(AttributionDisposition.UNRESOLVED))
@patch("auto_coder.pr_processor.CloudManager.read_bindings_strict", return_value={})
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
    with patch("auto_coder.pr_processor.CloudManager.read_bindings_strict", return_value={"42": MagicMock(provider="jules", task_id="task")}):
        decision = _select_review_repair_route("owner/repo", _pr(), client)

    assert decision.disposition is ReviewRepairRouteDisposition.CONFLICT
    assert "exact-PR cloud association" in decision.reason


def test_failed_authoritative_read_is_unavailable_not_cloud_fallback() -> None:
    client = MagicMock()
    client.get_pull_request_routing_metadata_strict = MagicMock(side_effect=RuntimeError("offline"))

    decision = _select_review_repair_route("owner/repo", _pr(), client)

    assert decision.disposition is ReviewRepairRouteDisposition.UNAVAILABLE
    assert "offline" in decision.reason


def test_genuine_cloud_pr_with_empty_authoritative_body_keeps_cloud_route() -> None:
    client = _client(_metadata(""))

    decision = _select_review_repair_route("owner/repo", _pr(""), client)

    assert decision.disposition is ReviewRepairRouteDisposition.CLOUD
    assert decision.reason == "no authoritative explicit local declaration"
    assert decision.evidence == _metadata("")


@patch("auto_coder.pr_processor.resolve_codex_pr_origin", return_value=AttributionResult(AttributionDisposition.UNRESOLVED))
def test_linked_issue_binding_is_retained_and_cannot_override_local_route(_attribution, tmp_path) -> None:
    manager = CloudManager("owner/repo", cloud_file_path=tmp_path / "cloud.csv")
    issue_binding = CloudTaskBinding(provider="jules", task_id="linked-issue-session")
    assert manager.ensure_binding(7, issue_binding) is True
    client = _client(_metadata(), _metadata())

    with (
        patch("auto_coder.pr_processor.CloudManager", return_value=manager),
        patch("auto_coder.pr_processor._resolve_cloud_task_origin") as cloud_origin,
    ):
        result = _delegate_cloud_review_thread_repair("owner/repo", _pr(), client)

    assert result.route_disposition == "LOCAL_REQUIRED"
    assert result.delivered is False
    assert "owner/repo PR #42 at owner/repo:issue-7_attempt-1@live-sha" in result[0]
    cloud_origin.assert_not_called()
    assert manager.get_binding(7) == issue_binding
    assert manager.get_binding(42) is None


@patch("auto_coder.pr_processor.resolve_codex_pr_origin", return_value=AttributionResult(AttributionDisposition.UNRESOLVED))
@patch("auto_coder.pr_processor.CloudManager.read_bindings_strict", return_value={})
def test_changed_authoritative_head_invalidates_selected_local_route(_binding, _attribution) -> None:
    changed_head = replace(_metadata(), head_sha="changed-live-sha")
    client = _client(_metadata(), changed_head)

    with patch("auto_coder.pr_processor._resolve_cloud_task_origin") as cloud_origin:
        result = _delegate_cloud_review_thread_repair("owner/repo", _pr(), client)

    assert result.route_disposition == "CONFLICT"
    assert result.delivered is False
    assert result == ["Review repair routing CONFLICT for PR #42: authoritative PR target or local declaration changed after route selection"]
    cloud_origin.assert_not_called()


def test_unreadable_exact_pr_binding_is_unavailable_at_selection_and_delegation(tmp_path) -> None:
    cloud_path = tmp_path / "cloud.csv"
    cloud_path.write_text("issue_number,provider,backend_name,session_id\n42,jules,,exact-pr-session\n", encoding="utf-8")
    manager = CloudManager("owner/repo", cloud_file_path=cloud_path)
    original_open = builtins.open

    def deny_cloud_read(path, *args, **kwargs):
        if str(path) == str(cloud_path):
            raise PermissionError("cloud ownership denied")
        return original_open(path, *args, **kwargs)

    with (
        patch("auto_coder.pr_processor.CloudManager", return_value=manager),
        patch("builtins.open", side_effect=deny_cloud_read),
        patch("auto_coder.pr_processor._resolve_cloud_task_origin") as cloud_origin,
    ):
        decision = _select_review_repair_route("owner/repo", _pr(), _client(_metadata()))
        result = _delegate_cloud_review_thread_repair("owner/repo", _pr(), _client(_metadata()))

    assert decision.disposition is ReviewRepairRouteDisposition.UNAVAILABLE
    assert "PermissionError: cloud ownership denied" in decision.reason
    assert result.route_disposition == "UNAVAILABLE"
    assert "PermissionError: cloud ownership denied" in result[0]
    assert "LOCAL_REQUIRED" not in result[0]
    cloud_origin.assert_not_called()


def test_malformed_unrelated_provider_history_does_not_change_local_route(tmp_path) -> None:
    manager = CloudManager("owner/repo", cloud_file_path=tmp_path / "cloud.csv")
    runs_path = tmp_path / "cloud_runs.json"
    runs_path.write_text(json.dumps({"7:bad": {"repo_name": "owner/repo", "issue_number": 7, "attempt": "not-an-integer", "provider": "claude-routine", "task_id": "old-claude-task"}}), encoding="utf-8")
    runs = CloudRunRepository("owner/repo", storage_path=runs_path)
    attributions = CodexPrAttributionRepository("owner/repo", storage_path=tmp_path / "attributions.json")

    with (
        patch("auto_coder.pr_processor.CloudManager", return_value=manager),
        patch("auto_coder.cloud_run.CloudRunRepository", return_value=runs),
        patch("auto_coder.pr_processor.CodexPrAttributionRepository", return_value=attributions),
        patch("auto_coder.pr_processor._resolve_cloud_task_origin") as cloud_origin,
    ):
        result = _delegate_cloud_review_thread_repair("owner/repo", _pr(), _client(_metadata(), _metadata()))

    assert result.route_disposition == "LOCAL_REQUIRED"
    assert result.delivered is False
    assert "owner/repo PR #42 at owner/repo:issue-7_attempt-1@live-sha" in result[0]
    assert json.loads(runs_path.read_text(encoding="utf-8"))["7:bad"]["attempt"] == "not-an-integer"
    cloud_origin.assert_not_called()
