"""Connector-shaped conformance fixtures for ChatGPT-authored adjudications."""

import json
import uuid
from pathlib import Path

from auto_coder.review_adjudication import AdjudicationStatus, Decision, parse_decision, render_decision
from auto_coder.review_adjudication_github import AdjudicationContextStore, AdjudicationSnapshot, IssueEvidence, PullRequestBinding, build_issue_contracts, new_context, reconcile_thread, render_context_projection
from auto_coder.review_adjudication_orchestrator import AdjudicationEffectStore, plan_adjudication_effects
from auto_coder.util.gh_cache import ReviewThread, ReviewThreadComment

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


def test_connector_replies_hydrate_and_plan_exact_downstream_effects(tmp_path: Path) -> None:
    issue = FIXTURE["issue"]
    contracts = build_issue_contracts([IssueEvidence(issue["id"], issue["number"], issue["title"], issue["body"])])
    binding = PullRequestBinding(FIXTURE["repository_id"], FIXTURE["repository"], FIXTURE["pr_number"], FIXTURE["head_sha"], FIXTURE["base_sha"], FIXTURE["base_ref"])

    for index, case in enumerate(FIXTURE["cases"]):
        thread = _root_thread()
        context = new_context(binding, thread, contracts)
        decision = Decision(str(uuid.uuid4()), context.context_id, context.head_sha, context.contract_digest, case["verdict"], case["directive"], (), "The complete two-concern root is assessed against Issue #2021 REQ-001.", "chatgpt-assisted")
        raw_reply = render_decision(decision)
        thread.comments.append(ReviewThreadComment(902 + index, raw_reply, "operator", FIXTURE["adjudicator_id"], "User", f"2026-09-0{index + 2}T00:00:00Z", f"2026-09-0{index + 2}T00:00:00Z", FIXTURE["root"]["comment_id"]))
        store = AdjudicationContextStore(tmp_path / f"reader-{index}.sqlite")
        ledger = store.register(context, f"observation-{index}")
        result = reconcile_thread(ledger, thread, [FIXTURE["adjudicator_id"]], [FIXTURE["root"]["author_id"]])
        snapshot = AdjudicationSnapshot(ledger.context, thread.comments[0].body, (issue["number"],), FIXTURE["root"]["author_id"], result.source_comment_id, result, f"observation-{index}")
        plan = plan_adjudication_effects([snapshot], AdjudicationEffectStore(tmp_path / f"effects-{index}.sqlite"))

        assert result.decision_id == decision.decision_id
        if case["effect"] == "repair":
            assert result.status is AdjudicationStatus.APPLICABLE
            assert len(plan.upholds) == 1 and not plan.overrules
            assert plan.upholds[0].rationale == decision.rationale
        elif case["effect"] == "retire-root-only":
            assert result.status is AdjudicationStatus.APPLICABLE
            assert len(plan.overrules) == 1 and not plan.upholds
            assert plan.overrules[0].gap_id is None
        else:
            assert result.status is AdjudicationStatus.UNDECIDED
            assert not plan
