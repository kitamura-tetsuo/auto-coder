"""Connector-shaped conformance fixtures for ChatGPT-authored adjudications."""

import json
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.cloud_manager import CloudTaskBinding
from auto_coder.pr_processor import _apply_review_adjudication_effects
from auto_coder.review_adjudication import Decision, parse_decision, render_decision
from auto_coder.review_adjudication_github import AdjudicationContextStore, IssueEvidence, PullRequestBinding, ReviewAdjudicationService, build_issue_contracts, new_context, reconcile_thread, render_context_projection
from auto_coder.util.gh_cache import GitHubClient, PullRequestRepairMetadata, ReviewThread, ReviewThreadComment

FIXTURE = json.loads((Path(__file__).parent / "fixtures/review_adjudication_workflow.json").read_text(encoding="utf-8"))


def _root_thread() -> ReviewThread:
    root = FIXTURE["root"]
    return ReviewThread(
        id=root["thread_id"],
        comments=[ReviewThreadComment(root["comment_id"], root["body"], "review-bot", root["author_id"], "Bot", root["created_at"], root["updated_at"])],
    )


def test_reader_projection_exposes_complete_writer_preflight(tmp_path: Path) -> None:
    issue = FIXTURE["issue"]
    contracts = build_issue_contracts([IssueEvidence(issue["id"], issue["number"], issue["title"], issue["body"])])
    binding = PullRequestBinding(FIXTURE["repository_id"], FIXTURE["repository"], FIXTURE["pr_number"], FIXTURE["head_sha"], FIXTURE["base_sha"], FIXTURE["base_ref"])
    context = new_context(binding, _root_thread(), contracts)
    store = AdjudicationContextStore(tmp_path / "projection-fixture.sqlite")
    ledger = store.register(context, "fixture-observation")
    result = reconcile_thread(ledger, _root_thread(), [FIXTURE["adjudicator_id"]], [FIXTURE["root"]["author_id"]])

    projection = render_context_projection(context, ledger.tips(), [FIXTURE["adjudicator_id"]], result, "fixture-observation")

    assert f"root `{FIXTURE['root']['comment_id']}`" in projection
    assert f"Root revision: `{FIXTURE['root']['updated_at']}`" in projection
    assert f"Permitted adjudicator IDs: {FIXTURE['adjudicator_id']}" in projection
    assert "Objective-scope identities:" in projection
    assert "Reader lifecycle result: `NONE`" in projection
    assert "Observation revision: `fixture-observation`" in projection
    assert parse_decision(projection.split("Copy, edit, and post this entire envelope as a direct reply:\n\n", 1)[1]).context_id == context.context_id


def _raw_thread_response(reply_body: str | None = None) -> dict:
    root = FIXTURE["root"]
    comments = [
        {
            "databaseId": root["comment_id"],
            "body": root["body"],
            "createdAt": root["created_at"],
            "updatedAt": root["updated_at"],
            "replyTo": None,
            "author": {"__typename": "Bot", "login": "review-bot", "databaseId": root["author_id"]},
        }
    ]
    if reply_body is not None:
        comments.append(
            {
                "databaseId": 902,
                "body": reply_body,
                "createdAt": "2026-09-02T00:00:00Z",
                "updatedAt": "2026-09-02T00:00:00Z",
                "replyTo": {"databaseId": root["comment_id"]},
                "author": {"__typename": "User", "login": "operator", "databaseId": FIXTURE["adjudicator_id"]},
            }
        )
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {
                                "id": root["thread_id"],
                                "isResolved": False,
                                "isOutdated": False,
                                "comments": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": comments},
                            }
                        ],
                    }
                }
            }
        }
    }


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["verdict"])
def test_connector_replies_hydrate_and_run_normal_same_head_effects(tmp_path: Path, monkeypatch, case: dict) -> None:
    issue = FIXTURE["issue"]
    monkeypatch.setenv("AUTO_CODER_REVIEW_ADJUDICATION_DB", str(tmp_path / "reader.sqlite"))
    monkeypatch.setenv("AUTO_CODER_REVIEW_ADJUDICATION_EFFECTS_DB", str(tmp_path / "effects.sqlite"))
    client = GitHubClient("fixture-token")
    client.graphql_query = MagicMock(return_value=_raw_thread_response())
    client.get_issue_dispatch_snapshot_strict = MagicMock(return_value=issue)
    client.reply_to_review_thread = MagicMock()
    client.resolve_review_thread = MagicMock()
    metadata = {
        "number": FIXTURE["pr_number"],
        "head": {"ref": "feature", "sha": FIXTURE["head_sha"]},
        "base": {"ref": FIXTURE["base_ref"], "sha": FIXTURE["base_sha"], "repo": {"id": FIXTURE["repository_id"]}},
    }
    client.get_pull_request_metadata_strict = MagicMock(return_value=metadata)
    client.get_pull_request_repair_metadata_strict = MagicMock(return_value=PullRequestRepairMetadata(head_ref="feature", head_sha=FIXTURE["head_sha"], base_ref=FIXTURE["base_ref"]))
    service = ReviewAdjudicationService(client, AdjudicationContextStore(tmp_path / "reader.sqlite"))

    issued = service.refresh(FIXTURE["repository"], FIXTURE["pr_number"], metadata, [issue["number"]], [FIXTURE["root"]["author_id"]], [FIXTURE["adjudicator_id"]])[0]
    assert issued.context is not None
    assert "Copy, edit, and post" in client.reply_to_review_thread.call_args.args[3]
    decision = Decision(
        str(uuid.uuid4()),
        issued.context.context_id,
        FIXTURE["head_sha"],
        issued.context.contract_digest,
        case["verdict"],
        case["directive"],
        (),
        "The complete two-concern root is assessed against Issue #2021 REQ-001.",
        "chatgpt-assisted",
    )
    outgoing = render_decision(decision)
    client.reply_to_review_thread(FIXTURE["repository"], FIXTURE["pr_number"], FIXTURE["root"]["comment_id"], outgoing)
    client.graphql_query.return_value = _raw_thread_response(outgoing)

    hydrated = client.get_pr_review_threads_strict(FIXTURE["repository"], FIXTURE["pr_number"])
    assert hydrated[0].comments[1].author_id == FIXTURE["adjudicator_id"]
    assert hydrated[0].comments[1].in_reply_to_id == FIXTURE["root"]["comment_id"]
    admitted = service.refresh(FIXTURE["repository"], FIXTURE["pr_number"], metadata, [issue["number"]], [FIXTURE["root"]["author_id"]], [FIXTURE["adjudicator_id"]])[0]
    assert admitted.result.decision_id == decision.decision_id

    with (
        patch("auto_coder.pr_processor.get_pr_review_allowlist_from_config", return_value=[FIXTURE["root"]["author_id"]]),
        patch("auto_coder.pr_processor.get_review_adjudicator_allowlist_from_config", return_value=[FIXTURE["adjudicator_id"]]),
        patch("auto_coder.pr_processor.CloudManager.get_binding", return_value=CloudTaskBinding(provider="codex-cloud", task_id="task_fixture")),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as send_followup,
    ):
        actions, _ = _apply_review_adjudication_effects(FIXTURE["repository"], FIXTURE["pr_number"], metadata, client)

    if case["effect"] == "repair":
        assert send_followup.call_count == 1
        assert any("authorized bounded correction" in action for action in actions)
        client.resolve_review_thread.assert_not_called()
    elif case["effect"] == "retire-root-only":
        assert send_followup.call_count == 0
        assert any("Retired an overruled finding" in action for action in actions)
        client.resolve_review_thread.assert_called_once_with(FIXTURE["root"]["thread_id"])
    else:
        assert send_followup.call_count == 0
        assert not actions
        client.resolve_review_thread.assert_not_called()
