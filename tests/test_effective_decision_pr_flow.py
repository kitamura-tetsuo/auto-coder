"""Effective review decisions through shared production PR processing.

``_handle_pr_merge`` runs for real: the accepted finding comes from the production
Strong acceptance path, the ordinary review is the real ``run_adversarial_validation``
parse of a controlled backend response, review publication is the real
``GitHubAppReviewer`` over a scripted HTTP transport (every outbound request is
recorded), and the corrective handoff is observed at the provider boundary.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Iterator, Optional, Sequence
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.accepted_finding_bridge import RootObservation
from auto_coder.adversarial_validation_attempts import AdversarialValidationAttemptRepository
from auto_coder.adversarial_validator import run_adversarial_validation
from auto_coder.automation_config import AutomationConfig, ProcessedPRResult, PRProcessingOutcome
from auto_coder.canonical_pr_blocker_ledger import CanonicalPRBlockerLedger
from auto_coder.cli_helpers import AdversarialValidationAvailability
from auto_coder.cloud_task_client_base import CloudTask, CloudTaskState
from auto_coder.codex_wham_client import FollowUpDeliveryOutcome
from auto_coder.effective_decision_application import HANDOFF_DISPATCHED, HANDOFF_WAITING, WAIT_ROUTE_UNAVAILABLE, EffectiveDecisionStore
from auto_coder.github_app_reviewer import GitHubAppReviewer, ReviewerAppConfig, ReviewerAppIdentity
from auto_coder.pr_processor import (
    ClaimedReviewThreadGateState,
    CloudTaskOrigin,
    CloudTaskOriginResolution,
    ReviewRepairRouteDecision,
    ReviewRepairRouteDisposition,
    TwoTierGateInputs,
    _consume_pending_two_tier_publication,
    _handle_pr_merge,
    _review_feedback_identity,
)
from auto_coder.pr_review_execution import FindingDisposition, ReviewExecutionResult, ReviewMode, ScopeAssessment
from auto_coder.util.gh_cache import PullRequestRoutingMetadata, ReviewThread, ReviewThreadComment
from auto_coder.util.github_action import GitHubActionsStatusResult
from auto_coder.utils import CommandResult
from tests.test_accepted_finding_bridge import (  # noqa: F401  (env is a shared fixture)
    CONTRACT,
    POLICY,
    REPO,
    Env,
    _commit,
    accept_strong,
    env,
    finding_json,
    published_roots,
    save_empty_session,
)
from tests.test_effective_decision_application import RouterClient, reviewer_for

REVIEWER_LOGIN = "auto-coder-reviewer[bot]"


@dataclass
class ClosureScript:
    """The one ordinary reviewer's controlled closure assessment for the one accepted finding.

    ``calls`` holds the closure context the production path supplied to each
    ordinary invocation; a separate closure-only reviewer is never reachable.
    """

    status: str = "FIXED"  # FIXED | INVALID | STILL_VALID
    finding_id: str = "finding-a"
    observable: bool = True  # False: the authoritative target cannot be read when the evidence is applied
    scope: str = "BOUNDED"  # BOUNDED | EXPANDED | UNKNOWN
    omit_assessment: bool = False  # the reviewer returns no closure assessment at all
    calls: list[Any] = field(default_factory=list)
    closure_only_calls: list[Any] = field(default_factory=list)
    on_review: Any = None


class FakeProvider:
    """The cloud provider boundary: records every follow-up it is asked to deliver."""

    def __init__(self) -> None:
        self.followups: list[tuple[str, str]] = []
        self.accept = True
        self.completed_at: Optional[datetime] = None
        self.delivered_identities: set[str] = set()
        self.lose_next_response = False

    def send_followup(self, task_id: str, message: str, identities: Sequence[str] = ()) -> bool:
        self.followups.append((task_id, message))
        if self.accept:
            self.delivered_identities.update(identities)
        if self.lose_next_response:  # the provider accepted the request but the response never arrived
            self.lose_next_response = False
            raise ConnectionError("response lost")
        return self.accept

    def get_followup_delivery(self, task_id: str, identity: str) -> FollowUpDeliveryOutcome:
        return FollowUpDeliveryOutcome.DELIVERED if identity in self.delivered_identities else FollowUpDeliveryOutcome.NOT_DELIVERED

    def get_task(self, task_id: str) -> Optional[CloudTask]:
        if self.completed_at is None:
            return None
        return CloudTask(task_id=task_id, state=CloudTaskState.COMPLETED, updated_at=self.completed_at)


def ordinary_response(thread_id: str, status: str = "STILL_VALID", *, result: str = "PASS", evidence: str = "src/state.py:40 still drops state") -> str:
    return json.dumps(
        {
            "result": result,
            "summary": "All requirement coverage is verified.",
            "requirement_coverage": [{"requirement_id": "#2401/REQ-001", "status": "VERIFIED", "evidence": "The guard enforces the requirement."}],
            "findings": [],
            "test_oracle_gaps": [],
            "thread_dispositions": [{"thread_id": thread_id, "status": status, "rationale": "Re-inspected the current head.", "evidence": evidence}],
        }
    )


@dataclass
class Flow:
    """One PR whose accepted Strong finding is published and unresolved."""

    env: Env
    monkeypatch: pytest.MonkeyPatch
    pr: int
    origin: str = "cloud"
    provider_name: str = "jules"
    auto_status: bool = False
    saved_status: Optional[str] = "PASS"
    head: str = ""
    thread_id: str = "PRRT_accepted"
    root_id: int = 5391105725
    root_body: str = ""
    model_responses: list[str] = field(default_factory=list)
    provider: FakeProvider = field(default_factory=FakeProvider)
    router: Optional[RouterClient] = None
    local_executions: list[Any] = field(default_factory=list)
    merge: MagicMock = field(default_factory=MagicMock)
    client: MagicMock = field(default_factory=MagicMock)
    attempts_started: int = 0
    activity: dict[str, str] = field(default_factory=dict)
    model_calls: int = 0
    origin_available: bool = True
    exhausted: bool = False
    quota_hold: bool = False
    post_ci: Optional[GitHubActionsStatusResult] = None
    extra_threads: tuple[ReviewThread, ...] = ()
    resolve_on_approve: bool = False  # The GitHub boundary confirms resolution by the controller or after approval.
    thread_resolved: bool = False
    live_head: str = ""  # the live PR head when it differs from the head being processed
    current_base: str = ""  # the live PR base; defaults to the environment base and may advance while a reviewer runs

    def __post_init__(self) -> None:
        self.head = self.head or self.env.head
        self.current_base = self.current_base or self.env.base
        self.router = RouterClient(self.head)

    # -- arrangement ---------------------------------------------------

    def accept_finding(self, *, gap: bool = True, finding_id: str = "finding-a") -> None:
        save_empty_session(self.env, self.pr, self.head)
        inputs = accept_strong(self.env, self.pr, [finding_json(finding_id, gap=gap)])
        strong = self.env.cycle.snapshot(self.pr).accepted_strong_round
        assert strong is not None
        assert self.router is not None
        self.router.next_root_id = self.root_id
        reviewer = reviewer_for(self.env.tmp, self.monkeypatch, self.router)
        config = ReviewerAppConfig("4765828", "client", self.env.tmp / "reviewer.pem")
        with patch("auto_coder.pr_processor.load_reviewer_app_config", return_value=config), patch("auto_coder.pr_processor.GitHubAppReviewer", return_value=reviewer):
            published, reason = _consume_pending_two_tier_publication(REPO, self.pr, inputs)
        assert published, reason
        observation = reviewer.observe_authenticated_review_roots(REPO, self.pr, strong.head_sha)
        assert observation.complete and len(observation.roots) == 1
        self.root_body = observation.roots[0].body
        self.router.calls.clear()  # Only subsequent processing effects are counted by flow.reviews.
        self.gate_inputs = inputs

    def threads(self) -> tuple[ReviewThread, ...]:
        if not self.root_body:
            return self.extra_threads
        resolved = self.thread_resolved or (self.resolve_on_approve and any(review["event"] == "APPROVE" for review in self.reviews))
        return self.extra_threads + (ReviewThread(id=self.thread_id, is_resolved=resolved, comments=[ReviewThreadComment(database_id=self.root_id, author_login=REVIEWER_LOGIN, body=self.root_body)]),)

    def pr_data(self) -> dict[str, Any]:
        body = "<!-- auto-coder:local-llm -->\nFixes #2401" if self.origin == "local" else "Fixes #2401"
        return {"number": self.pr, "body": body, "labels": [], "head": {"ref": "feature-branch", "sha": self.head}, "base": {"ref": "main", "sha": self.env.base}}

    # -- execution -----------------------------------------------------

    def run(self, *, force: bool = False, thread_state: Optional[ClaimedReviewThreadGateState] = None, saved: Optional[tuple[Optional[str], Optional[str]]] = None, closure: Optional[ClosureScript] = None) -> list[str]:
        mp = self.monkeypatch
        env = self.env
        client = self.client
        client.get_pr_review_threads_strict.side_effect = lambda *_a, **_k: list(self.threads())
        client.resolve_review_thread.side_effect = lambda thread: self._set_thread_resolved(thread, self.resolve_on_approve)
        client.unresolve_review_thread.side_effect = lambda thread: self._set_thread_resolved(thread, False)
        client.get_pr_comments.return_value = []
        client.get_pr_reviews_strict.return_value = []
        client.get_pull_request_head_sha_strict.side_effect = lambda *_a, **_k: self.live_head or self.head
        client.get_pull_request.side_effect = lambda *_a, **_k: {"head": {"sha": self.live_head or self.head}, "base": {"sha": self.current_base}}
        client.get_pull_request_metadata_strict.side_effect = lambda *_a, **_k: {**self.pr_data(), "head": {"ref": "feature-branch", "sha": self.live_head or self.head}, "base": {"ref": "main", "sha": self.current_base}, "state": "open"}

        def current_state(*_a: object, **_k: object) -> ClaimedReviewThreadGateState:
            if thread_state is not None:
                return thread_state
            live = tuple(thread for thread in self.threads() if not thread.is_resolved)
            return ClaimedReviewThreadGateState(unresolved=live, blocking_unresolved=live, has_blocking_unresolved=bool(live))

        manager = MagicMock()
        manager.get_current_backend_identity.return_value = ("reviewer", "codex", "strong")
        manager._last_session_id = "provider-session"
        manager._last_continue_session_resumed = True
        responses = self.model_responses

        supplied: list[Any] = []

        def next_response(*_args: object, **_kwargs: object) -> str:
            self.model_calls += 1
            raw = responses.pop(0) if len(responses) > 1 else responses[0]
            if closure is None or not supplied:
                return raw
            if closure.on_review is not None:
                closure.on_review()
            payload = json.loads(raw)
            payload["closure_assessment"] = {
                "result": "FINDINGS" if closure.status == "STILL_VALID" else "PASS",
                "findings": [],
                "dispositions": [{"finding_id": closure.finding_id, "status": "OPEN" if closure.status == "STILL_VALID" else closure.status, "evidence": f"Independent exact-finding {closure.status} evidence at {self.head[:8]}"}],
                "scope": closure.scope,
                "scope_evidence": "Only the repair and its regression changed.",
            }
            if closure.omit_assessment:
                del payload["closure_assessment"]
            return json.dumps(payload)

        manager.continue_session.side_effect = next_response

        def real_validation(*args: Any, **kwargs: Any):
            from tests.test_accepted_finding_bridge import _context

            with (
                patch("auto_coder.adversarial_validator.build_adversarial_validation_context", return_value=_context()),
                patch("auto_coder.adversarial_validator.run_llm_prompt", side_effect=next_response),
            ):
                kwargs.pop("claimed_review_threads_section", None)
                kwargs.pop("execution_cwd", None)  # the controlled worktree is not a git checkout
                if closure is not None and kwargs.get("closure_input") is not None:
                    closure.calls.append(kwargs["closure_input"])
                    supplied.append(kwargs["closure_input"])
                return run_adversarial_validation(*args, backend_manager=manager, session_registry=env.registry, claimed_review_threads_section="", **kwargs)

        key = env.tmp / "reviewer.pem"
        key.write_text("fake", encoding="utf-8")
        mp.setattr("auto_coder.github_app_reviewer.jwt.encode", lambda *a, **k: "jwt")
        real_reviewer = GitHubAppReviewer

        def reviewer_factory(config: ReviewerAppConfig, ledger: Optional[CanonicalPRBlockerLedger] = None) -> GitHubAppReviewer:
            return real_reviewer(config, ledger=ledger, api_url="https://api.github.com", client=self.router, clock=lambda: 1000.0)

        reviewer_factory._anchored_comment = GitHubAppReviewer._anchored_comment  # type: ignore[attr-defined]  # static helpers stay reachable through the patched class name

        @contextlib.contextmanager
        def worktree(*_a: object, **_k: object) -> Iterator[str]:
            yield str(env.worktree)

        evidence = PullRequestRoutingMetadata("https://api.github.com", REPO, self.pr, "open", self.pr_data()["body"], REPO, "feature-branch", self.head)
        route = ReviewRepairRouteDecision(ReviewRepairRouteDisposition.LOCAL_REQUIRED if self.origin == "local" else ReviewRepairRouteDisposition.CLOUD, "controlled", evidence if self.origin == "local" else None)
        patches = [
            patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True),
            patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"}),
            patch("auto_coder.pr_processor._check_github_actions_status", side_effect=self._ci_results()),
            patch("auto_coder.pr_processor._get_claimed_review_thread_state", side_effect=current_state),
            patch("auto_coder.pr_processor.resolve_reviewer_app_identity", return_value=ReviewerAppIdentity(login=REVIEWER_LOGIN, app_id=4765828)),
            patch("auto_coder.pr_processor._get_adversarial_validation_eligibility", return_value=__import__("auto_coder.pr_processor", fromlist=["AdversarialValidationEligibility"]).AdversarialValidationEligibility(issue_numbers=(2401,))),
            patch("auto_coder.pr_processor._get_codex_review_state", return_value=__import__("auto_coder.pr_processor", fromlist=["CodexReviewState"]).CodexReviewState()),
            patch("auto_coder.pr_processor._get_published_adversarial_validation_status", side_effect=lambda *_a, **_k: saved or (self._published_status(), None)),
            patch("auto_coder.pr_processor._get_published_adversarial_validation_comment", return_value=("<!-- report -->", None)),
            patch("auto_coder.pr_processor.isolated_pr_head_worktree", worktree),
            patch("auto_coder.pr_processor.run_adversarial_validation", real_validation),
            patch("auto_coder.pr_processor.default_accepted_finding_bridge", lambda repo: env.bridge()),
            patch("auto_coder.adversarial_validator.default_accepted_finding_bridge", lambda repo: env.bridge()),
            patch("auto_coder.github_app_reviewer.load_reviewer_app_config", return_value=ReviewerAppConfig("4765828", "client", key)),
            patch("auto_coder.github_app_reviewer.GitHubAppReviewer", reviewer_factory),
            patch("auto_coder.pr_processor._observe_codex_cloud_remediation_activity", side_effect=lambda *_a, **_k: self.activity if self.provider_name == "codex-cloud" else None),  # production: only Codex Cloud has a completed-turn observer
            patch("auto_coder.pr_processor._select_review_repair_route", return_value=route),
            patch("auto_coder.pr_processor._revalidate_local_review_repair_route", return_value=route),
            patch("auto_coder.pr_processor._resolve_cloud_task_origin", return_value=CloudTaskOriginResolution(origin=CloudTaskOrigin(self.provider_name, "task-1", self.provider)) if self.origin_available else CloudTaskOriginResolution(reason="the configured origin has no route")),
            patch("auto_coder.pr_processor._revalidate_cloud_origin", return_value=None),
            patch("auto_coder.pr_processor._merge_pr", self.merge),
            patch("auto_coder.pr_processor.get_linked_issues_context", return_value="REQ-001: Preserve accepted findings."),
            patch("auto_coder.local_review_repair.admit_local_repair_allowance", return_value=(object(), "")),
            patch("auto_coder.local_review_repair.execute_local_review_repair", side_effect=self._local_execution),
        ]
        if self.exhausted:
            exhaustion = MagicMock(is_exhausted=True, exhausted_blocker_ids=("blk-exhausted",))
            patches.append(patch("auto_coder.pr_processor.check_pr_repair_exhaustion", return_value=exhaustion))
            patches.append(patch("auto_coder.pr_processor.publish_exhaustion_comment_deduped"))
        if self.quota_hold:
            from auto_coder.claude_followup_waits import ClaudeFollowupHoldActive

            patches.append(patch("auto_coder.pr_processor._send_followup_with_quota_admission", side_effect=ClaudeFollowupHoldActive(4_000_000_000.0)))
        if closure is not None:
            patches.extend(self._closure_patches(closure, manager, reviewer_factory, key))
        config = AutomationConfig()
        config.AUTO_MERGE = True
        config.ENABLE_ADVERSARIAL_VALIDATION = True
        self.status = ProcessedPRResult(pr_data=self.pr_data())
        with contextlib.ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            actions = _handle_pr_merge(client, REPO, self.pr_data(), config, {}, self.status, force_adversarial_validation=force)
        return list(actions)

    def _ci_results(self) -> list[GitHubActionsStatusResult]:
        """Pre-validation CI is green; later reads (the post-validation refresh) may differ."""
        green = GitHubActionsStatusResult(success=True, ids=[1])
        return [green, *([self.post_ci] * 8 if self.post_ci is not None else [green] * 8)]

    def _set_thread_resolved(self, thread: str, resolved: bool) -> None:
        assert thread == self.thread_id
        self.thread_resolved = resolved

    def _published_status(self) -> Optional[str]:
        """What GitHub durably holds: the newest review this test's transport accepted, else the arranged status."""
        if not self.auto_status:
            return self.saved_status
        from auto_coder.pr_processor import _parse_adversarial_validation_status

        accepted = [review for review in self.reviews if f"adversarial-validation:v11:{self.head}" in review["body"]]
        return _parse_adversarial_validation_status(accepted[-1]["body"]) if accepted else self.saved_status

    def _closure_patches(self, script: ClosureScript, manager: MagicMock, reviewer_factory: Any, key: Any) -> list[Any]:
        env = self.env
        head = self.head

        def execute(review_input: Any, _backend: Any, _cwd: Any) -> ReviewExecutionResult:
            if review_input.mode is ReviewMode.STRONG_AUDIT:  # the independent Strong-completion gate stays unsatisfied
                return ReviewExecutionResult(
                    mode=ReviewMode.STRONG_AUDIT,
                    round_id=review_input.round_id,
                    attempt_id=review_input.attempt_id,
                    head_sha=head,
                    base_sha=env.base,
                    contract_identity=CONTRACT.identity,
                    policy_identity=POLICY.identity,
                    finding_set_revision=0,
                    reviewer_provenance="strong/model",
                    verdict="ERROR",
                    diagnostic="strong reviewer is unavailable",
                )
            script.closure_only_calls.append(review_input)  # a closure-only reviewer must never be reachable
            raise AssertionError("a separate closure-only reviewer invocation is forbidden")

        def observe(_client: object, _repo: object, pr_data: Any, *_a: object, **_k: object) -> TwoTierGateInputs:
            if not script.observable:
                raise ConnectionError("authoritative target is unavailable")
            return TwoTierGateInputs(self.gate_inputs.gate, CONTRACT, POLICY, pr_data["head"]["sha"], pr_data["base"]["sha"])

        return [
            patch("auto_coder.pr_processor._two_tier_gate_inputs", side_effect=observe),
            patch("auto_coder.cli_helpers.resolve_adversarial_validation_availability", return_value=AdversarialValidationAvailability(backend_manager=MagicMock())),
            patch("auto_coder.pr_processor.CommandExecutor.run_command", side_effect=lambda *_a, **_k: CommandResult(True, "cumulative repair\n", "", 0)),
            patch("auto_coder.pr_processor.execute_review", side_effect=execute),
            patch("auto_coder.pr_processor.load_reviewer_app_config", return_value=ReviewerAppConfig("4765828", "client", key)),
            patch("auto_coder.pr_processor.GitHubAppReviewer", reviewer_factory),
        ]

    def _local_execution(self, request: Any, **_kwargs: Any):
        from auto_coder.local_review_repair import LocalReviewRepairOutcome

        self.local_executions.append(request)
        return LocalReviewRepairOutcome("awaiting_validation", "published", True, True)

    # -- observation ---------------------------------------------------

    @property
    def reviews(self) -> list[dict[str, Any]]:
        assert self.router is not None
        return self.router.posted_reviews

    @property
    def handoffs(self) -> int:
        """Distinct corrective requests: the local route's durable dedupe key is its feedback identity set."""
        return len({request.feedback_identities for request in self.local_executions}) if self.origin == "local" else len(self.provider.followups)

    def handoff_text(self) -> str:
        if self.origin == "local":
            return self.local_executions[-1].prompt
        return self.provider.followups[-1][1]

    def retained(self):
        return EffectiveDecisionStore(REPO).load(self.pr)


@pytest.fixture
def flow_env(env: Env) -> Env:  # noqa: F811
    """Share the production default ledger so every participant sees one canonical store."""
    return replace(env, ledger=CanonicalPRBlockerLedger())


def make_flow(flow_env: Env, monkeypatch: pytest.MonkeyPatch, pr: int, origin: str, **kwargs: Any) -> Flow:
    monkeypatch.setenv("HOME", str(flow_env.tmp))
    flow = Flow(flow_env, monkeypatch, pr, origin=origin, **kwargs)
    return flow


# -- AS-001 -----------------------------------------------------------------


@pytest.mark.parametrize("origin", ["cloud", "local"])
@pytest.mark.parametrize(("pr", "gap", "expected"), [(5438, True, "NEEDS_TESTS"), (917, False, "NEEDS_FIX")])
def test_contradictory_raw_pass_becomes_actionable_repair_and_never_approves(flow_env: Env, monkeypatch: pytest.MonkeyPatch, origin: str, pr: int, gap: bool, expected: str) -> None:
    flow = make_flow(flow_env, monkeypatch, pr, origin)
    flow.accept_finding(gap=gap)
    flow.model_responses = [ordinary_response(flow.thread_id)]
    attempts_before = AdversarialValidationAttemptRepository(REPO).latest_sequence(pr, flow.head)

    actions = flow.run()

    assert [review["event"] for review in flow.reviews] == ["REQUEST_CHANGES"], (actions, flow.router.calls)
    review = flow.reviews[0]
    assert f"adversarial validation: {expected}" in review["body"]
    if not gap:  # an implementation correction is not a test-oracle gap, so the model's PASS was contradicted by the decision
        assert "Raw model verdict `PASS` is historical diagnostic evidence only" in review["body"]
    assert "comments" not in review  # the existing root is referenced; no duplicate root is created
    assert "represented by existing review threads" in review["body"]
    assert all(post["event"] != "APPROVE" for post in flow.reviews)

    retained = flow.retained()
    assert retained is not None and retained.status == expected and retained.publication == "CONFIRMED" and retained.handoff == HANDOFF_DISPATCHED
    snapshot = flow_env.ledger.get_snapshot("https://api.github.com", REPO, pr)
    assert len(snapshot.blockers) == 1 and retained.blocker_ids == [snapshot.blockers[0].blocker_id]

    assert flow.handoffs == 1
    handoff = flow.handoff_text()
    assert "Counterexample for finding-a" in handoff or "finding-a" in handoff
    assert "#2401/REQ-001" in handoff or "REQ-001" in handoff
    if gap:
        assert "Add only the focused regression protection" in handoff or "regression" in handoff.lower()
    assert flow.merge.call_count == 0
    assert flow.status.outcome is PRProcessingOutcome.DEFERRED  # an enqueued repair is not completion
    assert AdversarialValidationAttemptRepository(REPO).latest_sequence(pr, flow.head) == attempts_before + 1


# -- AS-002 -----------------------------------------------------------------


@pytest.mark.parametrize("origin", ["cloud", "local"])
@pytest.mark.parametrize("mode", ["automatic", "single", "force"])
def test_saved_pass_never_clears_an_open_accepted_finding_and_pending_work_reaches_its_route_once(flow_env: Env, monkeypatch: pytest.MonkeyPatch, origin: str, mode: str) -> None:
    flow = make_flow(flow_env, monkeypatch, 6100, origin)
    flow.accept_finding()
    flow.model_responses = [ordinary_response(flow.thread_id)]

    actions = flow.run(force=mode == "force")

    assert [review["event"] for review in flow.reviews] == ["REQUEST_CHANGES"], (actions, flow.router.calls)
    assert flow.handoffs == 1 and flow.merge.call_count == 0

    # Reconstructed: a same-head review is now the newest non-pass verdict; repeated processing
    # (even with a different TOG alias or reviewer session) must not issue a second provider request.
    flow.saved_status = "NEEDS_TESTS"
    flow.model_responses = [ordinary_response(flow.thread_id, evidence="a differently worded observation")]
    again = flow.run()
    assert flow.handoffs == 1, again
    assert [review["event"] for review in flow.reviews] == ["REQUEST_CHANGES"]  # no new review, no cached-headline approval
    assert flow.merge.call_count == 0


def test_undispatched_correction_still_reaches_its_route_without_a_new_commit(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow = make_flow(flow_env, monkeypatch, 6101, "cloud", saved_status="NEEDS_TESTS")
    flow.accept_finding()
    flow.provider.accept = False  # the provider refuses the first delivery
    flow.model_responses = [ordinary_response(flow.thread_id)]
    first = flow.run()
    assert flow.merge.call_count == 0 and not any("Skipping merge" in action and "approve" in action.lower() for action in first)
    assert flow.provider.followups  # the request was attempted at the provider boundary
    flow.provider.accept = True
    flow.run()
    assert flow.merge.call_count == 0


# -- AS-003 -----------------------------------------------------------------


def test_unavailable_accepted_state_retains_reconciliation_without_inventing_repair_and_recovers_normally(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow = make_flow(flow_env, monkeypatch, 6200, "cloud")
    flow.accept_finding()
    flow.model_responses = [ordinary_response(flow.thread_id)]
    good_bridge = flow_env.bridge

    broken = MagicMock()
    broken.project.side_effect = RuntimeError("accepted-state read failed")
    monkeypatch.setattr(Env, "bridge", lambda self, checkpoint=None: broken)
    first = flow.run()
    assert flow.handoffs == 0, first  # no invented test/code repair
    assert all(review["event"] != "APPROVE" for review in flow.reviews)
    retained = flow.retained()
    assert retained is not None and retained.next_action == "RECONCILIATION" and retained.status == "BLOCKED" and retained.wait_reason
    assert flow.status.outcome is PRProcessingOutcome.DEFERRED and flow.merge.call_count == 0

    reviews_before = len(flow.reviews)
    flow.saved_status = "BLOCKED"
    flow.model_responses = [ordinary_response(flow.thread_id)]
    waiting = flow.run()  # unchanged evidence: wait without repeating model work or repair
    assert flow.handoffs == 0 and len(flow.reviews) == reviews_before
    assert any("waiting for reconciliation evidence" in action for action in waiting), waiting

    monkeypatch.setattr(Env, "bridge", good_bridge)  # the same accepted state is readable again, same SHA, no --force
    flow.model_responses = [ordinary_response(flow.thread_id)]
    flow.run()
    assert flow.handoffs == 1
    recovered = flow.retained()
    assert recovered is not None and recovered.status == "NEEDS_TESTS" and recovered.next_action == "FOCUSED_TEST_REPAIR"
    assert [review["event"] for review in flow.reviews][-1] == "REQUEST_CHANGES"


# -- AS-004 -----------------------------------------------------------------


@contextlib.contextmanager
def race_at_the_authorization_point(monkeypatch: pytest.MonkeyPatch, race: Any) -> Iterator[list[str]]:
    """Run ``race`` (another participant's committed change) exactly once, immediately before approval authorization."""
    from auto_coder.effective_decision_application import ApprovalAuthority

    original_fence = ApprovalAuthority.transition_fence
    log: list[str] = []

    def racing_fence(self: ApprovalAuthority) -> Any:
        if not log:
            log.append("raced")
            race()
        return original_fence(self)

    monkeypatch.setattr(ApprovalAuthority, "transition_fence", racing_fence)
    yield log


def test_race_with_a_newly_accepted_blocker_is_rejected_then_consumed_as_a_test_repair(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    pr = 6300
    flow = make_flow(flow_env, monkeypatch, pr, "cloud", saved_status=None)
    save_empty_session(flow_env, pr, flow.head)
    flow.model_responses = [ordinary_response("none", status="ADDRESSED")]

    def another_participant_accepts_and_publishes_a_blocker() -> None:
        inputs = accept_strong(flow_env, pr, [finding_json("finding-race")], head=flow.head)
        strong = flow_env.cycle.snapshot(pr).accepted_strong_round
        assert strong is not None
        inputs.gate.state.acknowledge_publication(pr, strong.round_id)
        observation = published_roots(flow_env, pr, monkeypatch, root_ids={"finding-race": 7777})
        flow.root_body, flow.root_id, flow.thread_id = observation.roots[0].body, 7777, "PRRT_race"

    with race_at_the_authorization_point(monkeypatch, another_participant_accepts_and_publishes_a_blocker) as raced:
        actions = flow.run()

    assert raced == ["raced"]
    assert [review["event"] for review in flow.reviews] == ["REQUEST_CHANGES"], actions  # the old APPROVE never reached the endpoint
    assert any("refused before transmission" in action for action in actions)
    assert not any("Published APPROVE" in action for action in actions)
    retained = flow.retained()
    assert retained is not None and retained.status == "NEEDS_TESTS" and retained.publication == "CONFIRMED" and retained.handoff == HANDOFF_DISPATCHED
    assert flow.handoffs == 1 and flow.merge.call_count == 0 and flow.status.outcome is PRProcessingOutcome.DEFERRED


def test_race_that_leaves_authority_unavailable_retains_reconciliation_for_the_next_run(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    pr = 6301
    flow = make_flow(flow_env, monkeypatch, pr, "cloud", saved_status=None)
    save_empty_session(flow_env, pr, flow.head)
    flow.model_responses = [ordinary_response("none", status="ADDRESSED")]

    def accepted_state_becomes_unreadable() -> None:
        flow_env.cycle.storage_path.parent.mkdir(parents=True, exist_ok=True)
        flow_env.cycle.storage_path.write_text("{ unreadable", encoding="utf-8")

    with race_at_the_authorization_point(monkeypatch, accepted_state_becomes_unreadable) as raced:
        actions = flow.run()

    assert raced == ["raced"]
    assert all(review["event"] != "APPROVE" for review in flow.reviews), actions
    assert flow.handoffs == 0  # no invented repair from an unreadable authority
    retained = flow.retained()
    assert retained is not None and retained.next_action == "RECONCILIATION" and retained.status == "BLOCKED"
    assert flow.merge.call_count == 0 and flow.status.outcome is PRProcessingOutcome.DEFERRED


def test_newer_registered_attempt_stops_the_old_participant_and_a_fresh_read_cannot_restore_its_authority(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    pr = 6302
    flow = make_flow(flow_env, monkeypatch, pr, "cloud", saved_status=None)
    save_empty_session(flow_env, pr, flow.head)
    flow.model_responses = [ordinary_response("none", status="ADDRESSED")]
    attempts = AdversarialValidationAttemptRepository(REPO)

    def newer_attempt_registered() -> None:
        # Registered by another participant before this one's authorization check; the
        # registration itself (pending or later failed) is what supersedes the old attempt.
        with attempts._locked():
            state = attempts._read()
            sequence = int(state["next_sequence"])  # type: ignore[call-overload]
            state["attempts"].append({"id": "newer", "sequence": sequence, "pr_number": pr, "head_sha": flow.head, "status": "IN_PROGRESS", "started_at": 0.0})  # type: ignore[union-attr]
            state["next_sequence"] = sequence + 1
            attempts._write(state)

    with race_at_the_authorization_point(monkeypatch, newer_attempt_registered) as raced:
        actions = flow.run()

    assert raced == ["raced"]
    assert flow.reviews == [], actions  # neither the old APPROVE nor a replacement review was transmitted
    assert any("no longer holds current application authority" in action for action in actions)
    assert flow.handoffs == 0 and flow.merge.call_count == 0
    assert attempts.latest_sequence(pr, flow.head) == 2


# -- AS-005 -----------------------------------------------------------------


def _repair_to(flow: Flow, label: str) -> str:
    """A distinct repaired head H2; the corrective request has completed and pushed it."""
    flow.head = _commit(flow.env.worktree, label)
    flow.router.head_sha = flow.head  # type: ignore[union-attr]
    return flow.head


@pytest.mark.parametrize("origin", ["cloud", "local"])
def test_repaired_head_closes_the_finding_through_the_ordinary_producer_before_any_pass_exists(flow_env: Env, monkeypatch: pytest.MonkeyPatch, origin: str) -> None:
    flow = make_flow(flow_env, monkeypatch, 7100, origin)
    flow.accept_finding()
    flow.model_responses = [ordinary_response(flow.thread_id)]
    flow.run()  # AS-001: the accepted finding is upheld and its correction handed to the originating route
    assert flow.handoffs == 1 and [review["event"] for review in flow.reviews] == ["REQUEST_CHANGES"]

    h2 = _repair_to(flow, "repair-head")
    flow.saved_status = None  # no review exists yet for the repaired head
    flow.model_responses = [ordinary_response(flow.thread_id, status="ADDRESSED", evidence="tests/test_state.py asserts the invariant")]
    script = ClosureScript(status="FIXED")
    actions = flow.run(closure=script)

    assert len(script.calls) == 1, actions  # closure was reached although no effective PASS existed
    snapshot = flow_env.cycle.snapshot(7100)
    assert snapshot.open_findings == () and snapshot.accepted_closure is not None and snapshot.accepted_closure.head_sha == h2
    blocker = flow_env.ledger.get_snapshot("https://api.github.com", REPO, 7100).blockers[0]
    assert blocker.disposition.value == "VERIFIED_CORRECTION"

    events = [review["event"] for review in flow.reviews]
    assert events[-1] == "APPROVE" and "APPROVE" not in events[:-1], events  # PASS is published only after the closure was accepted
    assert flow.handoffs == 1  # no further corrective request was needed
    retained = flow.retained()
    assert retained is not None and retained.status == "PASS" and retained.head_sha == h2 and retained.publication == "CONFIRMED"
    assert flow.merge.call_count == 0  # a verified review is not a merge: Strong completion is still a separate gate


@pytest.mark.parametrize("outcome", ["INVALID", "STILL_VALID"])
def test_completed_no_change_at_the_same_head_converges_or_retains_the_same_blocker(flow_env: Env, monkeypatch: pytest.MonkeyPatch, outcome: str) -> None:
    flow = make_flow(flow_env, monkeypatch, 7200 if outcome == "INVALID" else 7201, "cloud")
    flow.accept_finding()
    flow.model_responses = [ordinary_response(flow.thread_id)]
    flow.run()
    assert flow.handoffs == 1
    first_blockers = [b.blocker_id for b in flow_env.ledger.get_snapshot("https://api.github.com", REPO, flow.pr).blockers]

    flow.saved_status = "NEEDS_TESTS"
    request_identity = _review_feedback_identity(f"{REPO}#{flow.pr}:{flow.provider_name}:task-1:", flow.threads()[0], 0)
    flow.activity = {request_identity: "completed-turn-no-commit"}  # the provider reports the request completed; the head is unchanged
    flow.provider.completed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    flow.model_responses = [ordinary_response(flow.thread_id, status="ADDRESSED", evidence="tests/test_state.py asserts the invariant")]
    script = ClosureScript(status=outcome)
    actions = flow.run(closure=script)

    assert len(script.calls) == 1, actions
    assert flow.head == flow_env.head  # no dummy commit
    assert [b.blocker_id for b in flow_env.ledger.get_snapshot("https://api.github.com", REPO, flow.pr).blockers] == first_blockers
    events = [review["event"] for review in flow.reviews]
    if outcome == "INVALID":
        assert flow_env.cycle.snapshot(flow.pr).open_findings == ()
        assert events[-1] == "APPROVE" and "APPROVE" not in events[:-1], events
        retained = flow.retained()
        assert retained is not None and retained.status == "PASS"
        assert flow.handoffs == 1
    else:
        assert [item.finding_id for item in flow_env.cycle.snapshot(flow.pr).open_findings] == ["finding-a"]
        assert "APPROVE" not in events and events[-1] == "REQUEST_CHANGES", events
        retained = flow.retained()
        assert retained is not None and retained.status == "NEEDS_TESTS"
        assert flow.handoffs == 2, actions  # the completed request was reassessed; a further correction is admitted for the same blocker
        # the closure attempt is spent for this corrective generation: repeating does not spend another independent review
        flow.run(closure=script)
        assert len(script.calls) == 1
    assert flow.merge.call_count == 0


# -- AS-006 -----------------------------------------------------------------


class SimulatedCrash(BaseException):
    """A process interruption: nothing after this point in the invocation runs."""


def _unfinished_flow(flow_env: Env, monkeypatch: pytest.MonkeyPatch, pr: int, **kwargs: Any) -> Flow:
    flow = make_flow(flow_env, monkeypatch, pr, kwargs.pop("origin", "cloud"), saved_status=None, auto_status=True, **kwargs)
    flow.accept_finding()
    flow.model_responses = [ordinary_response(flow.thread_id)]
    return flow


def test_interruption_after_decision_retention_before_publication_resumes_without_duplicate_effects(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow = _unfinished_flow(flow_env, monkeypatch, 8100)
    assert flow.router is not None

    def crash() -> None:
        flow.router.before_review = lambda: None  # type: ignore[union-attr]
        raise SimulatedCrash

    flow.router.before_review = crash
    with pytest.raises(SimulatedCrash):
        flow.run()
    assert flow.reviews == [] and flow.handoffs == 0
    retained = flow.retained()
    assert retained is not None and retained.status == "NEEDS_TESTS" and retained.publication == "PENDING" and retained.handoff == "PENDING"

    flow.run()  # reconstructed participants: normal processing at the same head
    assert [review["event"] for review in flow.reviews] == ["REQUEST_CHANGES"]
    assert flow.handoffs == 1
    final = flow.retained()
    assert final is not None and final.publication == "CONFIRMED" and final.handoff == HANDOFF_DISPATCHED and final.blocker_ids == retained.blocker_ids


def test_accepted_review_with_lost_response_is_reconciled_not_replayed(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    flow = _unfinished_flow(flow_env, monkeypatch, 8101)
    assert flow.router is not None
    flow.router.review_failure = httpx.ConnectError("response lost", request=httpx.Request("POST", "https://api.github.test/reviews"))

    actions = flow.run()

    assert [review["event"] for review in flow.reviews] == ["REQUEST_CHANGES"]  # transmitted exactly once
    assert any("Reconciled adversarial review publication" in action for action in actions), actions
    retained = flow.retained()
    assert retained is not None and retained.publication == "CONFIRMED" and retained.handoff == HANDOFF_DISPATCHED
    assert flow.handoffs == 1  # the corrective handoff proceeds from the reconciled, not replayed, review


def test_interruption_after_confirmed_review_before_corrective_handoff_keeps_the_obligation(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow = _unfinished_flow(flow_env, monkeypatch, 8102)
    import auto_coder.pr_processor as processor

    real_dispatch = processor._dispatch_effective_correction
    crashed: list[bool] = []

    def crash_once(*args: Any, **kwargs: Any):
        if not crashed:
            crashed.append(True)
            raise SimulatedCrash
        return real_dispatch(*args, **kwargs)

    monkeypatch.setattr(processor, "_dispatch_effective_correction", crash_once)
    with pytest.raises(SimulatedCrash):
        flow.run()
    assert [review["event"] for review in flow.reviews] == ["REQUEST_CHANGES"] and flow.handoffs == 0
    interrupted = flow.retained()
    assert interrupted is not None and interrupted.publication == "CONFIRMED" and interrupted.handoff == "PENDING"

    flow.run()  # the confirmed review is reused; only the undispatched repair is sent
    assert [review["event"] for review in flow.reviews] == ["REQUEST_CHANGES"]
    assert flow.handoffs == 1
    final = flow.retained()
    assert final is not None and final.handoff == HANDOFF_DISPATCHED and final.blocker_ids == interrupted.blocker_ids


def test_provider_request_with_lost_response_is_reconciled_by_its_retained_identity(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow = _unfinished_flow(flow_env, monkeypatch, 8103, provider_name="codex-cloud")
    flow.provider.lose_next_response = True

    first = flow.run()
    assert len(flow.provider.followups) == 1  # the accepted request whose response was lost
    retained = flow.retained()
    assert retained is not None and retained.handoff == HANDOFF_WAITING, first  # unknown delivery is never reported as confirmed

    flow.run()  # next normal processing reconciles through the transport's own delivery observation
    assert len(flow.provider.followups) == 1  # no duplicate request
    final = flow.retained()
    assert final is not None and final.handoff == HANDOFF_DISPATCHED
    assert [review["event"] for review in flow.reviews] == ["REQUEST_CHANGES"]


def test_unconfirmed_decision_write_prevents_every_dependent_effect(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow = _unfinished_flow(flow_env, monkeypatch, 8104)

    def fail_write(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(EffectiveDecisionStore, "_write", fail_write)
    actions = flow.run()

    assert flow.reviews == [] and flow.handoffs == 0 and flow.merge.call_count == 0, actions
    assert any("could not be retained" in action and "no dependent review effect was emitted" in action for action in actions)
    assert flow.status.outcome is PRProcessingOutcome.FAILED
    monkeypatch.undo()
    assert flow.retained() is None


# -- AS-007 -----------------------------------------------------------------


def test_unavailable_origin_route_is_a_distinct_wait_and_the_next_run_consumes_the_pending_work(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow = _unfinished_flow(flow_env, monkeypatch, 9100, origin_available=False)

    actions = flow.run()

    assert [review["event"] for review in flow.reviews] == ["REQUEST_CHANGES"] and flow.handoffs == 0, actions
    retained = flow.retained()
    assert retained is not None and retained.handoff == HANDOFF_WAITING and retained.wait_reason == WAIT_ROUTE_UNAVAILABLE
    assert flow.status.outcome is not PRProcessingOutcome.SUCCESS and flow.merge.call_count == 0
    assert len(flow_env.ledger.get_snapshot("https://api.github.com", REPO, 9100).blockers) == 1  # no alternative blocker or route was invented

    flow.origin_available = True  # a transient routing failure ends; the same original request is delivered
    flow.run()
    final = flow.retained()
    assert final is not None and final.handoff == HANDOFF_DISPATCHED and final.blocker_ids == retained.blocker_ids
    assert flow.handoffs == 1 and [review["event"] for review in flow.reviews] == ["REQUEST_CHANGES"]


def test_quota_deferral_retains_the_original_request_without_delivery_or_new_scope(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow = _unfinished_flow(flow_env, monkeypatch, 9101, quota_hold=True)

    actions = flow.run()

    assert flow.provider.followups == [] and flow.merge.call_count == 0, actions
    retained = flow.retained()
    assert retained is not None and retained.handoff == HANDOFF_WAITING and retained.wait_reason == "QUOTA_DEFERRED" and retained.status == "NEEDS_TESTS"
    assert flow.status.outcome is PRProcessingOutcome.DEFERRED
    assert [review["event"] for review in flow.reviews] == ["REQUEST_CHANGES"]
    assert len(flow_env.ledger.get_snapshot("https://api.github.com", REPO, 9101).blockers) == 1


def test_exhausted_correction_allowance_is_an_explicit_stop_not_approval_or_new_scope(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow = _unfinished_flow(flow_env, monkeypatch, 9102, exhausted=True)

    actions = flow.run()

    assert flow.provider.followups == [] and flow.merge.call_count == 0, actions
    retained = flow.retained()
    assert retained is not None and retained.handoff == HANDOFF_WAITING and retained.wait_reason == "ALLOWANCE_EXHAUSTED"
    assert all(review["event"] != "APPROVE" for review in flow.reviews)
    assert len(flow_env.ledger.get_snapshot("https://api.github.com", REPO, 9102).blockers) == 1  # no new blocker was created to obtain allowance


# -- AS-008 -----------------------------------------------------------------


def test_accepted_implementation_finding_keeps_its_bounded_correction_and_ignores_unadmitted_comments(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    stray = ReviewThread(id="PRRT_stray", comments=[ReviewThreadComment(database_id=1, author_login="someone", body="please rename this variable")])
    flow = make_flow(flow_env, monkeypatch, 9200, "cloud", extra_threads=(stray,))
    flow.accept_finding(gap=False)
    flow.model_responses = [ordinary_response(flow.thread_id)]

    flow.run()

    retained = flow.retained()
    assert retained is not None and retained.status == "NEEDS_FIX" and retained.next_action == "IMPLEMENTATION_REPAIR"
    snapshot = flow_env.ledger.get_snapshot("https://api.github.com", REPO, 9200)
    assert len(snapshot.blockers) == 1 and retained.blocker_ids == [snapshot.blockers[0].blocker_id]  # the stray comment is not a blocker
    assert [review["event"] for review in flow.reviews] == ["REQUEST_CHANGES"] and "comments" not in flow.reviews[0]
    handoff = flow.handoff_text()
    assert retained.blocker_ids[0] in handoff or "Actual behavior for finding-a" in handoff
    assert "Counterexample for finding-a" in handoff


@pytest.mark.parametrize("gate", ["ci", "strong"])
def test_a_verified_review_is_not_a_merge_when_an_independent_gate_remains_blocked(flow_env: Env, monkeypatch: pytest.MonkeyPatch, gate: str) -> None:
    pr = 9201 if gate == "ci" else 9202
    flow = make_flow(flow_env, monkeypatch, pr, "cloud", saved_status=None)
    save_empty_session(flow_env, pr, flow.head)  # no accepted findings: every review obligation is closed
    flow.model_responses = [ordinary_response("none", status="ADDRESSED")]
    flow.gate_inputs = TwoTierGateInputs(__import__("auto_coder.two_tier_pr_gate", fromlist=["TwoTierPrGate"]).TwoTierPrGate(REPO, flow_env.cycle), CONTRACT, POLICY, flow.head, flow_env.base)
    if gate == "ci":
        flow.post_ci = GitHubActionsStatusResult(success=False, in_progress=True, ids=[2])
    actions = flow.run(closure=ClosureScript() if gate == "strong" else None)

    assert [review["event"] for review in flow.reviews] == ["APPROVE"], actions  # the legitimate PASS path is published
    retained = flow.retained()
    assert retained is not None and retained.status == "PASS" and retained.publication == "CONFIRMED"
    assert flow.merge.call_count == 0  # no merge mutation: CI / Strong completion remain separate gates
    assert flow.status.outcome is not PRProcessingOutcome.SUCCESS


# -- AS-003 (ambiguous association) -----------------------------------------


def test_genuinely_ambiguous_association_spends_one_focused_attempt_per_evidence_revision(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    pr = 6400
    flow = make_flow(flow_env, monkeypatch, pr, "cloud", saved_status=None, auto_status=True)
    save_empty_session(flow_env, pr, flow.head)
    inputs = accept_strong(flow_env, pr, [finding_json("finding-a"), finding_json("finding-b", boundary="src/state.py:other", scenario="Another correction.")])
    strong = flow_env.cycle.snapshot(pr).accepted_strong_round
    assert strong is not None
    inputs.gate.state.acknowledge_publication(pr, strong.round_id)
    observation = published_roots(flow_env, pr, monkeypatch, root_ids={"finding-a": 5001, "finding-b": 5002})
    flow_env.bridge().project(flow_env.target(pr), RootObservation(roots=(observation.roots[1],), complete=True))  # finding-b owns root 5002
    flow.root_body, flow.root_id, flow.thread_id = observation.roots[0].body, 5002, "PRRT_ambiguous"  # finding-a's marker on finding-b's root
    flow.model_responses = [ordinary_response("PRRT_ambiguous")]

    first = flow.run()
    assert flow.model_calls == 1 and flow.handoffs == 0, first  # no repair from an uncertain mapping
    retained = flow.retained()
    assert retained is not None and retained.next_action == "RECONCILIATION" and retained.reconciliation_attempts == [retained.evidence_revision]
    assert [review["event"] for review in flow.reviews] == ["COMMENT"]  # a nonapproving diagnostic, never REQUEST_CHANGES from the uncertain mapping

    for _ in range(2):  # unchanged evidence: an explicit wait, no identical model or repair work
        waiting = flow.run()
        assert flow.model_calls == 1 and flow.handoffs == 0 and len(flow.reviews) == 1
        assert any("waiting for reconciliation evidence" in action for action in waiting), waiting
        assert flow.status.outcome is PRProcessingOutcome.DEFERRED

    flow_env.bridge().project(flow_env.target(pr), RootObservation(roots=(observation.roots[0],), complete=True))  # new exact association evidence for finding-a
    flow.root_body, flow.root_id, flow.thread_id = observation.roots[0].body, 5001, "PRRT_a"
    flow.model_responses = [ordinary_response("PRRT_a")]
    flow.run()
    assert flow.model_calls == 2  # eligible again on relevant new evidence
    resumed = flow.retained()
    assert resumed is not None and resumed.next_action != "RECONCILIATION" and resumed.status in {"NEEDS_TESTS", "NEEDS_FIX"}


# -- review findings on the first push --------------------------------------


def test_acceptance_after_the_authority_check_is_ordered_after_the_authorized_transmission(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    pr = 9300
    flow = make_flow(flow_env, monkeypatch, pr, "cloud", saved_status=None)
    save_empty_session(flow_env, pr, flow.head)
    flow.model_responses = [ordinary_response("none", status="ADDRESSED")]
    assert flow.router is not None
    outcome: dict[str, bool] = {}

    def concurrent_acceptance() -> None:
        accept_strong(flow_env, pr, [finding_json("finding-late")], head=flow.head)
        outcome["accepted"] = True

    worker = threading.Thread(target=concurrent_acceptance)

    def pause_before_transport() -> None:  # the authority callback already succeeded; the request is about to be sent
        worker.start()
        worker.join(timeout=1.0)
        outcome["blocked_until_send"] = worker.is_alive()

    flow.router.before_review = pause_before_transport
    flow.run()
    worker.join(timeout=30)

    assert outcome == {"blocked_until_send": True, "accepted": True}  # acceptance could not interleave between the check and the send
    assert [review["event"] for review in flow.reviews] == ["APPROVE"]  # it followed the authorized transmission


def test_failed_checkpoint_write_is_not_overwritten_by_the_precomputed_decision(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from auto_coder.reviewer_session_registry import ReviewerSessionRegistry

    pr = 9301
    flow = make_flow(flow_env, monkeypatch, pr, "cloud", saved_status=None)
    save_empty_session(flow_env, pr, flow.head)
    flow.model_responses = [ordinary_response("none", status="ADDRESSED")]

    def failing_save(self: ReviewerSessionRegistry, *_args: object, **_kwargs: object) -> None:
        raise OSError("registry unavailable")

    monkeypatch.setattr(ReviewerSessionRegistry, "save", failing_save)
    actions = flow.run()

    assert all(review["event"] != "APPROVE" for review in flow.reviews), actions
    assert flow.handoffs == 0 and flow.merge.call_count == 0
    assert flow.status.outcome is not PRProcessingOutcome.SUCCESS


def test_closure_proposal_superseded_by_a_head_change_during_external_review_is_not_accepted(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow = make_flow(flow_env, monkeypatch, 9302, "cloud")
    flow.accept_finding()
    flow.model_responses = [ordinary_response(flow.thread_id)]
    flow.run()
    h2 = _repair_to(flow, "h2")
    h3 = _commit(flow_env.worktree, "h3")
    flow.saved_status = None
    flow.model_responses = [ordinary_response(flow.thread_id, status="ADDRESSED", evidence="tests/test_state.py asserts the invariant")]
    script = ClosureScript(status="FIXED")
    script.on_review = lambda: setattr(flow, "live_head", h3)  # the PR advances while the reviewer runs

    flow.run(closure=script)

    assert len(script.calls) == 1
    snapshot = flow_env.cycle.snapshot(9302)
    assert snapshot.accepted_closure is None and [item.finding_id for item in snapshot.open_findings] == ["finding-a"]
    assert h2 != h3


def test_transient_target_outage_retains_the_assessment_and_resumes_without_a_model_call(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow = make_flow(flow_env, monkeypatch, 9303, "cloud")
    flow.accept_finding()
    flow.model_responses = [ordinary_response(flow.thread_id)]
    flow.run()
    request_identity = _review_feedback_identity(f"{REPO}#{flow.pr}:{flow.provider_name}:task-1:", flow.threads()[0], 0)
    flow.activity = {request_identity: "completed-turn"}
    flow.provider.completed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    flow.saved_status = "NEEDS_TESTS"
    flow.model_responses = [ordinary_response(flow.thread_id, status="ADDRESSED", evidence="tests/test_state.py asserts the invariant")]
    calls_before = flow.model_calls
    outage = ClosureScript(status="FIXED")
    outage.on_review = lambda: setattr(outage, "observable", False)  # the authoritative target becomes unreadable after the review returns
    flow.run(closure=outage)
    assert len(outage.calls) == 1 and flow.model_calls == calls_before + 1
    assert flow_env.cycle.snapshot(flow.pr).accepted_closure is None  # retained, not applied: an unavailable read never certifies
    assert [item.finding_id for item in flow_env.cycle.snapshot(flow.pr).open_findings] == ["finding-a"]

    flow.auto_status = True  # GitHub now holds the saved review for this head
    restored = ClosureScript(status="FIXED")
    calls_before = flow.model_calls
    actions = flow.run(closure=restored)  # normal processing, same head, no commit, no store reset

    # The retained assessment is applied from the durable source: no closure-only
    # reviewer runs and the same evidence is never re-requested with closure context.
    assert restored.calls == [] and restored.closure_only_calls == [], actions
    snapshot = flow_env.cycle.snapshot(flow.pr)
    assert snapshot.accepted_closure is not None and snapshot.open_findings == ()
    assert snapshot.accepted_closure.head_sha == flow.head


def test_jules_origin_completion_without_a_commit_reaches_reassessment_through_the_task_record(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow = make_flow(flow_env, monkeypatch, 9304, "cloud")  # provider "jules": no Codex completed-turn observer exists
    flow.accept_finding()
    flow.model_responses = [ordinary_response(flow.thread_id)]
    flow.run()
    retained = flow.retained()
    assert retained is not None and retained.handoff_observation == "task:"  # delivered while the task was still running

    flow.provider.completed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)  # the task record now reports completion; the head is unchanged
    flow.saved_status = "NEEDS_TESTS"
    flow.model_responses = [ordinary_response(flow.thread_id, status="ADDRESSED", evidence="tests/test_state.py asserts the invariant")]
    script = ClosureScript(status="INVALID")
    flow.run(closure=script)

    assert len(script.calls) == 1
    assert flow_env.cycle.snapshot(flow.pr).open_findings == ()
