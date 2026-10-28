"""Effective review decisions through shared production PR processing.

``_handle_pr_merge`` runs for real: the accepted finding comes from the production
Strong acceptance path, the ordinary review is the real ``run_adversarial_validation``
parse of a controlled backend response, review publication is the real
``GitHubAppReviewer`` over a scripted HTTP transport (every outbound request is
recorded), and the corrective handoff is observed at the provider boundary.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Iterator, Optional, Sequence
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.accepted_finding_bridge import RootObservation
from auto_coder.adversarial_validation_attempts import AdversarialValidationAttemptRepository
from auto_coder.adversarial_validator import run_adversarial_validation
from auto_coder.automation_config import AutomationConfig, ProcessedPRResult, PRProcessingOutcome
from auto_coder.canonical_pr_blocker_ledger import BlockerDisposition, CanonicalPRBlockerLedger
from auto_coder.ci_observation import (
    CIConclusion,
    CIObservationSnapshot,
    ObservationAvailability,
    ObservationRequest,
    ObservationSubject,
    WorkflowExecutionIdentity,
    WorkflowObservation,
)
from auto_coder.cli_helpers import AdversarialValidationAvailability
from auto_coder.cloud_task_client_base import CloudTask, CloudTaskState
from auto_coder.codex_wham_client import FollowUpDeliveryOutcome
from auto_coder.effective_decision_application import HANDOFF_DISPATCHED, HANDOFF_WAITING, WAIT_ROUTE_UNAVAILABLE, EffectiveDecisionStore, RetainedDecision
from auto_coder.github_app_reviewer import GitHubAppReviewer, ReviewerAppConfig, ReviewerAppIdentity
from auto_coder.pr_processor import (
    ClaimedReviewThreadGateState,
    CloudTaskOrigin,
    CloudTaskOriginResolution,
    PRReviewCoordinationResult,
    ReviewRepairRouteDecision,
    ReviewRepairRouteDisposition,
    TwoTierGateInputs,
    _consume_pending_two_tier_publication,
    _get_claimed_review_thread_state,
    _handle_pr_merge,
    _review_feedback_identity,
    process_pull_request,
)
from auto_coder.pr_review_execution import FindingDisposition, ReviewExecutionResult, ReviewMode, ScopeAssessment
from auto_coder.util.gh_cache import PullRequestRoutingMetadata, ReviewThread, ReviewThreadComment
from auto_coder.util.github_action import GitHubActionsStatusResult
from auto_coder.utils import CommandResult
from tests.ci_observation_fakes import authoritative_ci_status
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
CONTROLLER_LOGIN = "auto-coder-controller[bot]"


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
    verdict: str = ""  # optional independent closure verdict
    omit_assessment: bool = False  # the reviewer returns no closure assessment at all
    malformed_assessment: bool = False  # invalid extension must not erase ordinary evidence
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
    requirement_text: Optional[str] = None
    auto_status: bool = False
    saved_status: Optional[str] = "PASS"
    head: str = ""
    thread_id: str = "PRRT_accepted"
    root_id: int = 5391105725
    root_body: str = ""
    thread_replies: list[ReviewThreadComment] = field(default_factory=list)
    model_responses: list[str] = field(default_factory=list)
    provider: FakeProvider = field(default_factory=FakeProvider)
    router: Optional[RouterClient] = None
    local_executions: list[Any] = field(default_factory=list)
    merge: MagicMock = field(default_factory=MagicMock)
    client: MagicMock = field(default_factory=MagicMock)
    attempts_started: int = 0
    activity: dict[str, str] = field(default_factory=dict)
    model_calls: int = 0
    validation_sections: list[str] = field(default_factory=list)
    origin_available: bool = True
    exhausted: bool = False
    quota_hold: bool = False
    real_local_allowance: bool = False
    post_ci: Optional[GitHubActionsStatusResult] = None
    extra_threads: tuple[ReviewThread, ...] = ()
    resolve_on_approve: bool = False  # The GitHub boundary confirms resolution by the controller or after approval.
    thread_resolved: bool = False
    live_head: str = ""  # the live PR head when it differs from the head being processed
    current_base: str = ""  # the live PR base; defaults to the environment base and may advance while a reviewer runs
    production_thread_collection: bool = False

    def __post_init__(self) -> None:
        self.head = self.head or self.env.head
        self.current_base = self.current_base or self.env.base
        self.router = RouterClient(self.head)

    # -- arrangement ---------------------------------------------------

    def accept_finding(self, *, gap: bool = True, finding_id: str = "finding-a", boundary: str = "src/state.py:delete_two") -> None:
        save_empty_session(self.env, self.pr, self.head)
        inputs = accept_strong(self.env, self.pr, [finding_json(finding_id, gap=gap, boundary=boundary)])
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
        retained_ids = {thread.comments[0].database_id for thread in self.extra_threads if thread.comments} | {self.root_id}
        published = tuple(
            ReviewThread(id=f"PRRT_new_{root['id']}", is_resolved=False, comments=[ReviewThreadComment(database_id=root["id"], author_login=REVIEWER_LOGIN, body=root["body"])])
            for roots in (self.router.review_roots.values() if self.router is not None else ())
            for root in roots
            if root["id"] not in retained_ids
        )
        if not self.root_body:
            return self.extra_threads + published
        resolved = self.thread_resolved or (self.resolve_on_approve and any(review["event"] == "APPROVE" for review in self.reviews))
        comments = [
            ReviewThreadComment(
                database_id=self.root_id,
                author_id=4765828,
                author_login=REVIEWER_LOGIN,
                body=self.root_body,
            ),
            *(replace(reply) for reply in self.thread_replies),
        ]
        return self.extra_threads + published + (ReviewThread(id=self.thread_id, is_resolved=resolved, comments=comments),)

    def pr_data(self) -> dict[str, Any]:
        body = "<!-- auto-coder:local-llm -->\nFixes #2401" if self.origin == "local" else "Fixes #2401"
        return {"number": self.pr, "body": body, "labels": [], "head": {"ref": "feature-branch", "sha": self.head}, "base": {"ref": "main", "sha": self.env.base}}

    # -- execution -----------------------------------------------------

    def run(
        self,
        *,
        force: bool = False,
        thread_state: Optional[ClaimedReviewThreadGateState] = None,
        saved: Optional[tuple[Optional[str], Optional[str]]] = None,
        closure: Optional[ClosureScript] = None,
        public_origin: bool = False,
        thread_gate_enabled: Optional[bool] = None,
    ) -> Sequence[str]:
        mp = self.monkeypatch
        env = self.env
        client = self.client
        client.get_authenticated_user_login.return_value = CONTROLLER_LOGIN
        if self.production_thread_collection:
            client._production_threads = lambda: list(self.threads())
            setattr(
                type(client),
                "get_pr_review_threads_strict",
                lambda current, *_a, **_k: current._production_threads(),
            )
        else:
            client.get_pr_review_threads_strict.side_effect = lambda *_a, **_k: list(self.threads())
        client.reply_to_review_thread.side_effect = self._reply_to_thread
        client.update_review_thread_comment.side_effect = self._update_thread_comment
        client.resolve_review_thread.side_effect = lambda thread: self._set_thread_resolved(thread, self.resolve_on_approve)
        client.unresolve_review_thread.side_effect = lambda thread: self._set_thread_resolved(thread, False)
        client.get_pr_comments.return_value = []
        client.get_pr_reviews_strict.return_value = []
        client.get_pull_request_head_sha_strict.side_effect = lambda *_a, **_k: self.live_head or self.head
        client.get_pull_request.side_effect = lambda *_a, **_k: {"head": {"sha": self.live_head or self.head}, "base": {"sha": self.current_base}}
        client.get_pull_request_metadata_strict.side_effect = lambda *_a, **_k: {**self.pr_data(), "head": {"ref": "feature-branch", "sha": self.live_head or self.head}, "base": {"ref": "main", "sha": self.current_base}, "state": "open"}

        def current_state(*_a: object, **_k: object) -> ClaimedReviewThreadGateState:
            if self.production_thread_collection:
                return _get_claimed_review_thread_state(client, REPO, self.pr, config=AutomationConfig())
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
            if closure is not None and closure.on_review is not None:
                closure.on_review()
            if closure is None or not supplied:
                return raw
            payload = json.loads(raw)
            payload["closure_assessment"] = {
                "result": closure.verdict or ("FINDINGS" if closure.status == "STILL_VALID" else "PASS"),
                "findings": [],
                "dispositions": [{"finding_id": closure.finding_id, "status": "OPEN" if closure.status == "STILL_VALID" else closure.status, "evidence": f"Independent exact-finding {closure.status} evidence at {self.head[:8]}"}],
                "scope": closure.scope,
                "scope_evidence": "Only the repair and its regression changed.",
            }
            if closure.omit_assessment:
                del payload["closure_assessment"]
            elif closure.malformed_assessment:
                payload["closure_assessment"] = {"result": "PASS", "dispositions": "invalid"}
            return json.dumps(payload)

        manager.continue_session.side_effect = next_response

        def current_validation_context(repo_name: str, pr_data: dict[str, Any], *_args: object, **_kwargs: object):
            from auto_coder.adversarial_validator import IssueRequirement, _requirement_manifest_identity, _snapshot_digest
            from tests.test_accepted_finding_bridge import _context

            context = _context()
            context.repo_name = repo_name
            context.pr_number = int(pr_data["number"])
            context.pr_body = str(pr_data.get("body") or "")
            if self.requirement_text is not None:
                context.issue_context = f"Issue #2401 REQ-001: {self.requirement_text}"
                context.issue_requirements = [IssueRequirement("#2401/REQ-001", self.requirement_text)]
            files = [{"filename": path, "raw_diff_sha256": hashlib.sha256(context.pr_diff.encode()).hexdigest()} for path in context.all_changed_files]
            context.validation_snapshot = _snapshot_digest(repo_name, pr_data, files, context.issue_requirements, context.pr_diff)
            context.requirement_manifest_identity = _requirement_manifest_identity(context.issue_requirements)
            return context

        def real_validation(*args: Any, **kwargs: Any):
            with patch("auto_coder.adversarial_validator.run_llm_prompt", side_effect=next_response):
                section = kwargs.pop("claimed_review_threads_section", "")
                self.validation_sections.append(section)
                kwargs.pop("execution_cwd", None)  # the controlled worktree is not a git checkout
                if closure is not None and kwargs.get("closure_input") is not None:
                    closure.calls.append(kwargs["closure_input"])
                    supplied.append(kwargs["closure_input"])
                return run_adversarial_validation(*args, backend_manager=manager, session_registry=env.registry, claimed_review_threads_section=section, **kwargs)

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
            patch("auto_coder.pr_processor.build_adversarial_validation_context", side_effect=current_validation_context),
            patch("auto_coder.adversarial_validator.build_adversarial_validation_context", side_effect=current_validation_context),
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
            patch("auto_coder.pr_processor.get_linked_issues_context", side_effect=lambda *_a, **_k: current_validation_context(REPO, self.pr_data()).issue_context),
            patch("auto_coder.pr_correction_job.offer_pr_correction_job", side_effect=self._local_offer),
        ]
        if self.production_thread_collection:
            patches.append(
                patch(
                    "auto_coder.pr_processor._resolve_eligible_review_thread_ids",
                    return_value={4765828},
                )
            )
        if public_origin:
            label = MagicMock()
            label.__enter__.return_value = True
            patches.extend(
                [
                    patch(
                        "auto_coder.pr_processor._reject_unsafe_codex_cloud_pr",
                        return_value=SimpleNamespace(metadata_error=None, actions=[], authoritative_pr_data=self.pr_data(), closed=False),
                    ),
                    patch("auto_coder.pr_processor._link_codex_cloud_pr_to_issue", return_value=None),
                    patch("auto_coder.pr_processor._link_jules_pr_to_issue", return_value=True),
                    patch("auto_coder.pr_processor._close_empty_pr", return_value=SimpleNamespace(closed=False)),
                    patch("auto_coder.pr_processor._close_stale_jules_pr", return_value=SimpleNamespace(closed=False)),
                    patch("auto_coder.pr_processor._should_skip_waiting_for_jules", return_value=False),
                    patch("auto_coder.pr_processor.LabelManager", return_value=label),
                ]
            )
        if thread_gate_enabled is not None:
            patches.append(patch("auto_coder.pr_processor._is_pr_review_thread_gate_enabled", return_value=thread_gate_enabled))
        if not self.real_local_allowance:
            patches.append(patch("auto_coder.local_review_repair.admit_local_repair_allowance", return_value=(object(), "")))
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
            if public_origin:
                self.status = process_pull_request(client, config, REPO, self.pr_data(), force_adversarial_validation=force)
                return self.status.actions_taken
            actions = _handle_pr_merge(client, REPO, self.pr_data(), config, {}, self.status, force_adversarial_validation=force)
            assert isinstance(actions, PRReviewCoordinationResult)
            return actions

    def _ci_results(self):
        """Pre-validation CI is green; later reads (the post-validation refresh) may differ."""
        reads = 0

        def read(repository, pr_data, *args, **kwargs):
            nonlocal reads
            reads += 1
            configured = self.post_ci if reads > 1 and self.post_ci is not None else GitHubActionsStatusResult(success=True, ids=[1])
            return authoritative_ci_status(
                success=configured.success,
                ids=configured.ids,
                in_progress=configured.in_progress,
                error=configured.error,
                waiting_runs=configured.waiting_runs,
            )(repository, pr_data, *args, **kwargs)

        return read

    def _set_thread_resolved(self, thread: str, resolved: bool) -> None:
        assert thread == self.thread_id
        self.thread_resolved = resolved

    def _reply_to_thread(self, repository: str, pr_number: int, root_id: int, body: str) -> None:
        assert (repository, pr_number, root_id) == (REPO, self.pr, self.root_id)
        comment_id = max(comment.database_id or 0 for thread in self.threads() for comment in thread.comments) + 1
        self.thread_replies.append(ReviewThreadComment(database_id=comment_id, author_login=CONTROLLER_LOGIN, body=body, in_reply_to_id=root_id))

    def _update_thread_comment(self, repository: str, comment_id: int, body: str) -> None:
        assert repository == REPO
        matches = [comment for comment in self.thread_replies if comment.database_id == comment_id]
        assert len(matches) == 1
        matches[0].body = body

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

    def _local_offer(self, request: Any, _backend_name: str, **_kwargs: Any):
        self.local_executions.append(request)
        return MagicMock(job_id=f"job-{len(self.local_executions)}")

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


def test_unknown_canonical_reference_publishes_diagnostic_without_closing_threads(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #5783: a misspelled owner must not resolve threads then fail publication."""
    from auto_coder.dashboard_reviews import list_row
    from auto_coder.execution_trace import Outcome
    from auto_coder.pr_processor import _record_pr_stage
    from auto_coder.review_audit import ReviewAuditStore

    audit = ReviewAuditStore(flow_env.tmp / "review-audit")
    monkeypatch.setattr("auto_coder.review_capture.recorder._global_audit_store", audit)
    flow = make_flow(flow_env, monkeypatch, 5783, "cloud")
    flow.accept_finding(gap=False)
    before = flow_env.ledger.get_snapshot("https://api.github.com", REPO, flow.pr)
    payload = json.loads(ordinary_response(flow.thread_id, status="ADDRESSED", result="NEEDS_FIX"))
    payload["requirement_coverage"][0]["status"] = "VIOLATED"
    payload["findings"] = [
        {
            "requirement_id": "#2401/REQ-001",
            "finding_identity": "missing-production-regression",
            "evidence_classification": "DEMONSTRATED",
            "correction_identity": "canonical:blk_43a1256826",
            "correction_scope_fingerprint": "invented-scope",
            "correction_context_fingerprint": "invented-context",
            "violated_requirement": "Preserve state",
            "reachability": "src/state.py:delete_two",
            "required_behavior": "Preserve both values",
            "actual_behavior": "The second value is lost",
            "evidence": "The production entry point drops the second value",
            "counterexample": "Delete loses the second value while tests pass",
            "anchor_path": "src/state.py",
        }
    ]
    flow.model_responses = [json.dumps(payload)]

    with patch("auto_coder.pr_processor._record_pr_stage", wraps=_record_pr_stage) as record_stage:
        actions = flow.run(force=True)

    assert flow.model_calls == 1
    assert [review["event"] for review in flow.reviews] == ["COMMENT"], actions
    review = flow.reviews[0]
    assert "comments" not in review
    assert "correction_reference_invalid" in review["body"]
    assert "canonical:blk_43a1256826" in review["body"]
    assert "adversarial validation: ERROR" in review["body"]
    assert not any("publication blocked" in action or "Reopened" in action for action in actions), actions
    flow.client.resolve_review_thread.assert_not_called()
    flow.client.unresolve_review_thread.assert_not_called()
    assert flow.handoffs == 0
    flow.merge.assert_not_called()
    failures = [call for call in record_stage.call_args_list if call.args[1] == "pr.adversarial-validation" and call.args[3] == Outcome.FAILED]
    assert len(failures) == 1
    assert failures[0].args[4]["diagnostic_category"] == "correction_reference_invalid"
    history = audit.get_recent_history(REPO, target_type="pr", target_number=str(flow.pr), review_kind="pr_adversarial")
    assert len(history.records) == 1
    retained = history.records[0]
    assert list_row(retained).verdict == "ERROR"
    assert retained.native_report["diagnostic_category"] == "correction_reference_invalid"
    assert "canonical:blk_43a1256826" in retained.native_report["raw_response_preview"]
    assert any(effect.disposition == "confirmed" and effect.details.get("phase") == "publication" for effect in retained.effects)
    after = flow_env.ledger.get_snapshot("https://api.github.com", REPO, flow.pr)
    assert [(blocker.blocker_id, blocker.disposition) for blocker in after.blockers] == [(blocker.blocker_id, blocker.disposition) for blocker in before.blockers]

    # A fresh, valid same-head attempt can still uphold the real accepted owner.
    flow.saved_status = "ERROR"
    flow.model_responses = [ordinary_response(flow.thread_id)]
    retried = flow.run(force=True)
    assert flow.model_calls == 2
    assert [review["event"] for review in flow.reviews] == ["COMMENT", "REQUEST_CHANGES"], retried
    assert flow.handoffs == 1
    flow.merge.assert_not_called()


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
    assert retained is not None and retained.status == expected and retained.publication == "CONFIRMED" and retained.handoff == HANDOFF_DISPATCHED, "\n".join(actions)
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


def _historical_root_with_current_violation(flow_env: Env, monkeypatch: pytest.MonkeyPatch, origin: str, extension: str, *, historical_boundary: str = "src/state.py:delete_two") -> tuple[Flow, ClosureScript, dict[str, Any]]:
    """The incident's natural state: native resolution is not accepted Strong closure."""
    from tests.test_adversarial_validator import _demonstrated_finding_with_anchor

    flow = make_flow(flow_env, monkeypatch, 5746, origin, saved_status=None, auto_status=True, provider_name="codex-cloud")
    flow.accept_finding(gap=False, boundary=historical_boundary)
    _repair_to(flow, "historical-fix-with-independent-join-binding-defect")
    flow.thread_resolved = True
    raw = json.loads(ordinary_response(flow.thread_id, result="NEEDS_FIX"))
    raw["summary"] = "The historical counterexample is fixed, but a distinct current state-loss violation remains."
    raw["requirement_coverage"][0]["status"] = "VIOLATED"
    current_finding = _demonstrated_finding_with_anchor(40)
    current_finding["requirement_id"] = "#2401/REQ-001"
    raw["findings"] = [current_finding]
    raw["thread_dispositions"] = []  # The resolved historical root is not in the claimed native thread set.
    flow.model_responses = [json.dumps(raw)]
    script = ClosureScript(omit_assessment=extension == "missing", malformed_assessment=extension == "malformed")
    return flow, script, raw


def _reconstruct_flow_stores(flow: Flow) -> None:
    """Drop every process-local store/gate object while keeping durable transport history."""
    from auto_coder.two_tier_pr_gate import TwoTierPrGate

    flow.env = flow.env.reconstructed()
    flow.gate_inputs = replace(flow.gate_inputs, gate=TwoTierPrGate(REPO, flow.env.cycle))


@pytest.mark.parametrize("origin", ["cloud", "local"])
@pytest.mark.parametrize("extension", ["complete", "missing", "malformed"])
def test_resolved_historical_root_does_not_suppress_current_correction_or_repeat_reviews(flow_env: Env, monkeypatch: pytest.MonkeyPatch, origin: str, extension: str) -> None:
    from auto_coder import pr_processor
    from auto_coder.durable_repair_allowance import RepairAllowanceLedger

    flow, script, raw = _historical_root_with_current_violation(flow_env, monkeypatch, origin, extension)
    flow.real_local_allowance = origin == "local"
    observations: list[tuple[int, int, int]] = []
    local_allowance_snapshots = []

    with patch("auto_coder.pr_processor._record_pr_stage", wraps=pr_processor._record_pr_stage) as stages:
        for _ in range(3):
            actions = flow.run(closure=script)
            observations.append((flow.model_calls, len(flow.reviews), flow.handoffs))
            if origin == "local":
                local_allowance_snapshots.append(RepairAllowanceLedger().get_snapshot("https://api.github.com", REPO, flow.pr))
            raw["thread_dispositions"] = [{"thread_id": thread.id, "status": "STILL_VALID", "rationale": "The current correction is still required.", "evidence": "src/state.py:40 still drops state."} for thread in flow.threads() if not thread.is_resolved]
            flow.model_responses = [json.dumps(raw)]
            _reconstruct_flow_stores(flow)

    assert flow.handoffs == 1, (observations, list(actions))
    assert observations == [(1, 1, 1)] * 3
    assert len(flow.local_executions if origin == "local" else flow.provider.followups) == 1
    assert [review["event"] for review in flow.reviews] == ["REQUEST_CHANGES"]
    assert flow.router is not None and sum(len(roots) for roots in flow.router.review_roots.values()) == 2
    if origin == "local":
        assert len(local_allowance_snapshots[0].generations) == 1
        assert local_allowance_snapshots == [local_allowance_snapshots[0]] * 3
    assert flow.merge.call_count == 0 and flow.thread_resolved
    assert [finding.finding_id for finding in flow.env.cycle.snapshot(flow.pr).open_findings] == ["finding-a"]
    assert "State is discarded" in flow.handoff_text()
    assert "Counterexample for finding-a" not in flow.handoff_text()
    retained = flow.retained()
    assert retained is not None and retained.status == "NEEDS_FIX" and retained.handoff == HANDOFF_DISPATCHED
    assert retained.source_identities == []
    assert retained.ordinary_correction is not None
    restored = retained.ordinary_correction.restore()
    assert restored.result == "NEEDS_FIX" and len(restored.findings) == 1
    assert len(retained.pending_closure_source_identities if extension == "complete" else retained.pending_reconciliation_source_identities) == 1
    snapshot = flow.env.ledger.get_snapshot("https://api.github.com", REPO, flow.pr)
    assert len(snapshot.blockers) == 2
    assert any(blocker.blocker_id in flow.handoff_text() for blocker in snapshot.blockers if blocker.blocker_id in retained.blocker_ids)
    assert actions.disposition.value == "CORRECTIVE_HANDOFF"
    repair_events = [call.args for call in stages.call_args_list if call.args[1] == "pr.repair-delegation"]
    assert repair_events and all(event[4]["review_disposition"] == "CORRECTIVE_HANDOFF" for event in repair_events)


@pytest.mark.parametrize("extension", ["complete", "missing"])
def test_same_boundary_historical_owner_never_enters_current_canonical_repair_bundle(flow_env: Env, monkeypatch: pytest.MonkeyPatch, extension: str) -> None:
    flow, script, _ = _historical_root_with_current_violation(flow_env, monkeypatch, "cloud", extension, historical_boundary="src/state.py")
    historical = flow.env.bridge().project(flow.env.target(flow.pr, flow.head)).records[0]

    actions = flow.run(closure=script)

    assert flow.handoffs == 1, actions
    snapshot = flow.env.ledger.get_snapshot("https://api.github.com", REPO, flow.pr)
    assert len(snapshot.blockers) == 2
    assert {blocker.authoritative_boundary for blocker in snapshot.blockers} == {"src/state.py"}
    current = next(blocker for blocker in snapshot.blockers if blocker.blocker_id != historical.canonical_blocker_id)
    bundles = flow.env.ledger.get_repair_bundles_for_pr("https://api.github.com", REPO, flow.pr)
    assert len(bundles) == 1 and [blocker.blocker_id for blocker in bundles[0].blockers] == [current.blocker_id]
    retained = flow.retained()
    assert retained is not None and retained.blocker_ids == [current.blocker_id]
    assert historical.canonical_blocker_id not in flow.handoff_text()
    assert "Counterexample for finding-a" not in flow.handoff_text()
    assert flow.env.cycle.snapshot(flow.pr).open_findings[0].finding_id == "finding-a"
    assert flow.merge.call_count == 0


@pytest.mark.parametrize("origin", ["cloud", "local"])
@pytest.mark.parametrize("gap", [False, True])
def test_new_violation_is_handed_off_once_while_accepted_correction_awaits_closure(flow_env: Env, monkeypatch: pytest.MonkeyPatch, origin: str, gap: bool) -> None:
    from auto_coder import pr_processor
    from tests.test_adversarial_validator import _demonstrated_finding_with_anchor

    flow = make_flow(flow_env, monkeypatch, 5556, origin, saved_status=None, auto_status=True, provider_name="codex-cloud")
    flow.accept_finding(gap=gap)
    _repair_to(flow, "partial-enum-repair")
    raw = json.loads(ordinary_response(flow.thread_id, status="ADDRESSED", result="NEEDS_FIX", evidence="src/state.py:40 now guards the original path"))
    raw["summary"] = "The old counterexample is fixed, but a distinct reachable state-loss defect remains."
    raw["requirement_coverage"][0]["status"] = "VIOLATED"
    current_finding = _demonstrated_finding_with_anchor(40)
    current_finding["requirement_id"] = "#2401/REQ-001"
    raw["findings"] = [current_finding]
    flow.model_responses = [json.dumps(raw)]
    script = ClosureScript(omit_assessment=True)

    with patch("auto_coder.pr_processor._record_pr_stage", wraps=pr_processor._record_pr_stage) as stages:
        actions = flow.run(closure=script)

    assert flow.model_calls == 1 and flow.handoffs == 1, "\n".join(actions)
    assert [review["event"] for review in flow.reviews] == ["REQUEST_CHANGES"]
    retained = flow.retained()
    assert retained is not None and retained.status == "NEEDS_FIX" and retained.handoff == HANDOFF_DISPATCHED
    assert retained.source_identities == [] and len(retained.pending_closure_source_identities) == 1
    assert "State is discarded" in flow.handoff_text()
    assert "Counterexample for finding-a" not in flow.handoff_text()
    assert flow.merge.call_count == 0 and not flow.thread_resolved
    assert [item.finding_id for item in flow_env.cycle.snapshot(flow.pr).open_findings] == ["finding-a"]
    repair_events = [call.args for call in stages.call_args_list if call.args[1] == "pr.repair-delegation"]
    assert len(repair_events) == 1
    assert repair_events[0][4]["review_disposition"] == "CORRECTIVE_HANDOFF"
    assert actions.disposition.value == "CORRECTIVE_HANDOFF"

    assert flow.router is not None
    new_roots = flow.router.review_roots[max(flow.router.review_roots)]
    flow.extra_threads = tuple(ReviewThread(id=f"PRRT_new_{root['id']}", is_resolved=False, comments=[ReviewThreadComment(database_id=root["id"], author_login=REVIEWER_LOGIN, body=root["body"])]) for root in new_roots)
    again = flow.run(closure=script)
    assert flow.model_calls == 1 and flow.handoffs == 1 and len(flow.reviews) == 1, again
    assert flow.merge.call_count == 0 and not flow.thread_resolved
    assert again.disposition.value == "CORRECTIVE_HANDOFF"


def test_missing_closure_assessment_waits_at_the_same_head_without_repeating_validation(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow = make_flow(flow_env, monkeypatch, 5557, "cloud", saved_status=None, auto_status=True)
    flow.accept_finding(gap=False)
    _repair_to(flow, "fixed-with-missing-closure-assessment")
    flow.model_responses = [ordinary_response(flow.thread_id, status="ADDRESSED")]
    script = ClosureScript(omit_assessment=True)
    actions = flow.run(closure=script)
    retained = flow.retained()
    assert retained is not None and retained.next_action == "CLOSURE_ACCEPTANCE", actions
    assert retained.reconciliation_attempts == [retained.evidence_revision]

    for _ in range(2):
        waiting = flow.run(closure=script)
        assert flow.model_calls == 1 and flow.handoffs == 0 and len(flow.reviews) == 1, waiting
        assert any("waiting for reconciliation evidence" in action for action in waiting)
    assert flow.merge.call_count == 0 and not flow.thread_resolved

    flow.run(force=True, closure=script)
    assert flow.model_calls == 2 and flow.handoffs == 0


@pytest.mark.parametrize("mutation", ["same-source-observation", "unrelated-owner"])
def test_historical_reconciliation_wait_ignores_nonsemantic_ledger_writes(flow_env: Env, monkeypatch: pytest.MonkeyPatch, mutation: str) -> None:
    from auto_coder.canonical_pr_blocker_ledger import BlockerAdmissionPayload, CorrectionScope, EvidenceAvailability, QualifiedRequirement

    flow, script, raw = _historical_root_with_current_violation(flow_env, monkeypatch, "cloud", "missing")
    raw["result"] = "PASS"
    raw["summary"] = "The current requirement coverage is verified; accepted historical closure is still pending."
    raw["requirement_coverage"][0]["status"] = "VERIFIED"
    raw["findings"] = []
    flow.model_responses = [json.dumps(raw)]
    first = flow.run(closure=script)
    retained = flow.retained()
    assert retained is not None and retained.next_action == "RECONCILIATION", first
    snapshot = flow.env.ledger.get_snapshot("https://api.github.com", REPO, flow.pr)
    original_revision = snapshot.ledger_revision
    if mutation == "same-source-observation":
        snapshot = flow.env.ledger.record_evidence(
            "https://api.github.com",
            REPO,
            flow.pr,
            "repeat-observation-with-no-new-adjudication",
            snapshot.ledger_revision,
            snapshot.blockers[0].blocker_id,
            EvidenceAvailability.KNOWN,
            "The same original accepted source was observed again.",
            reviewed_head_sha=flow.head,
            reviewed_base_sha=flow.env.base,
        )
    else:
        _, snapshot = flow.env.ledger.admit_blocker(
            "https://api.github.com",
            REPO,
            flow.pr,
            "unrelated-reconciliation-source",
            snapshot.ledger_revision,
            BlockerAdmissionPayload(
                category="implementation",
                qualified_requirements=(QualifiedRequirement(9999, "REQ-099"),),
                authoritative_boundary="src/another_feature.py:unrelated",
                incorrect_behavior_or_missing_invariant="An independent state machine needs its own correction.",
                required_correction_outcome="Correct that independent state machine.",
                accepted_scope=CorrectionScope("Preserve an unrelated obligation."),
            ),
        )
    assert snapshot.ledger_revision > original_revision
    _reconstruct_flow_stores(flow)
    flow.model_responses = []

    waiting = flow.run(closure=script)

    assert flow.model_calls == 1 and flow.handoffs == 0 and len(flow.reviews) == 1, waiting
    assert waiting.disposition.value == "RECONCILIATION_WAIT"
    assert flow.retained().evidence_revision == retained.evidence_revision
    assert flow.env.cycle.snapshot(flow.pr).open_findings and flow.merge.call_count == 0


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("origin", ["cloud", "local"])
def test_saved_blocked_violation_recovers_to_repair_before_force_can_start_validation(flow_env: Env, monkeypatch: pytest.MonkeyPatch, force: bool, origin: str) -> None:
    from auto_coder import pr_processor
    from auto_coder.effective_decision_application import DecisionApplication
    from auto_coder.effective_review_decision import EffectiveNextAction
    from tests.test_adversarial_validator import _demonstrated_finding_with_anchor

    flow = make_flow(flow_env, monkeypatch, 5556, origin, saved_status=None, auto_status=True, provider_name="codex-cloud")
    flow.accept_finding(gap=False)
    _repair_to(flow, "saved-partial-enum-repair")
    payload = json.loads(ordinary_response(flow.thread_id, status="ADDRESSED", result="NEEDS_FIX"))
    payload["requirement_coverage"][0]["status"] = "VIOLATED"
    current = _demonstrated_finding_with_anchor(40)
    current["requirement_id"] = "#2401/REQ-001"
    payload["findings"] = [current]
    native_thread_id = "PRRT_current_native_finding"
    flow.extra_threads = (ReviewThread(id=native_thread_id, is_resolved=False, comments=[ReviewThreadComment(database_id=5391105999, author_login=REVIEWER_LOGIN, body="### Auto-Coder adversarial finding\n\nState is discarded in the existing native finding with different wording.")]),)
    payload["thread_dispositions"].append({"thread_id": native_thread_id, "status": "STILL_VALID", "rationale": "The current finding remains valid.", "evidence": "src/state.py:40 still drops state."})
    flow.model_responses = [json.dumps(payload)]
    script = ClosureScript(finding_id="missing-exact-finding")
    derive = pr_processor._derive_effective_application

    def historical_closure_priority(*args: Any, **kwargs: Any) -> DecisionApplication:
        application = derive(*args, **kwargs)
        assert application is not None
        return DecisionApplication(replace(application.result, result="BLOCKED"), replace(application.decision, status="BLOCKED", next_action=EffectiveNextAction.CLOSURE_ACCEPTANCE), application.projection, application.ordinary_result)

    with patch("auto_coder.pr_processor._derive_effective_application", side_effect=historical_closure_priority):
        first = flow.run(closure=script)
    assert flow.model_calls == 1 and flow.handoffs == 0, first
    assert [review["event"] for review in flow.reviews] == ["COMMENT"]
    original = flow.retained()
    assert original is not None and original.status == "BLOCKED"
    attempts = AdversarialValidationAttemptRepository(REPO)
    sequence = attempts.latest_sequence(flow.pr, flow.head)
    flow.model_responses = []  # no reviewer response exists: any invocation is a regression

    with patch("auto_coder.pr_processor._dispatch_effective_correction", wraps=pr_processor._dispatch_effective_correction) as dispatch, patch("auto_coder.pr_processor._record_pr_stage", wraps=pr_processor._record_pr_stage) as stages:
        recovered = flow.run(force=force, closure=script)

    assert dispatch.call_count == 1
    assert f"#### `{native_thread_id}`: STILL_VALID" in dispatch.call_args.args[4]
    replay_stages = [call for call in stages.call_args_list if call.args[1] == "pr.repair-delegation" and call.args[4].get("effect") == "adversarial-feedback-replay"]
    assert len(replay_stages) == 1
    assert replay_stages[0].args[4]["additional_model_execution"] is False
    assert replay_stages[0].args[4]["source_attempt_sequence"] == sequence

    assert flow.model_calls == 1 and flow.handoffs == 1, "\n".join(recovered)
    assert any("no new adversarial validation was started" in action for action in recovered)
    assert attempts.latest_sequence(flow.pr, flow.head) == sequence
    assert len(flow.reviews) == 1 and flow.merge.call_count == 0 and not flow.thread_resolved
    retained = flow.retained()
    assert retained is not None and retained.status == "NEEDS_FIX" and retained.handoff == HANDOFF_DISPATCHED
    assert "State is discarded" in flow.handoff_text() and "Counterexample for finding-a" not in flow.handoff_text()
    flow.run(force=force, closure=script)
    assert flow.model_calls == 1 and flow.handoffs == 1


def test_ledger_ahead_reconciliation_resumes_independent_closure_without_approval_shortcut(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow = make_flow(flow_env, monkeypatch, 7103, "local", saved_status="BLOCKED")
    flow.accept_finding()
    h2 = _repair_to(flow, "locally-verified-repair")
    snapshot = flow_env.ledger.get_snapshot("https://api.github.com", REPO, flow.pr)
    flow_env.ledger.record_transition(
        "https://api.github.com",
        REPO,
        flow.pr,
        "local-verification",
        snapshot.ledger_revision,
        snapshot.blockers[0].blocker_id,
        BlockerDisposition.VERIFIED_CORRECTION,
        "Independent local verifier accepted this exact correction",
        reviewed_head_sha=h2,
    )
    flow.thread_resolved = True
    EffectiveDecisionStore(REPO).retain(
        RetainedDecision(
            repository=REPO,
            pr_number=flow.pr,
            head_sha=h2,
            status="BLOCKED",
            next_action="RECONCILIATION",
            evidence_revision="previous-consumer",
            reconciliation_attempts=["previous-consumer"],
            publication="CONFIRMED",
        )
    )
    flow.model_responses = [ordinary_response(flow.thread_id, status="ADDRESSED")]
    # Expanded scope requires renewed Strong completion after closure; the
    # fixture deliberately leaves that independent reviewer unavailable.
    script = ClosureScript(status="FIXED", scope="EXPANDED")
    from auto_coder import pr_processor

    with patch("auto_coder.pr_processor._record_pr_stage", wraps=pr_processor._record_pr_stage) as stages:
        actions = flow.run(closure=script)

    assert flow.model_calls == 1 and len(script.calls) == 1, actions
    accepted = flow_env.cycle.snapshot(flow.pr)
    assert accepted.open_findings == () and accepted.requires_new_strong_round
    assert accepted.accepted_closure is None  # Expanded scope cannot certify merge completion.
    assert len(accepted.closures) == 1 and accepted.closures[0].head_sha == h2
    assert flow.handoffs == 0
    assert [review["event"] for review in flow.reviews] == ["APPROVE"]
    retained = flow.retained()
    assert retained is not None and retained.status == "PASS"
    assert flow.merge.call_count == 0  # Strong completion and final CI remain separate gates.
    closure_events = [call for call in stages.call_args_list if call.args[1] == "pr.ordinary-closure"]
    assert any(call.args[3].value == "completed" and call.args[4]["additional_model_execution"] is False for call in closure_events)
    assert not any(call.args[1] == "pr.merge-completion" for call in stages.call_args_list)


def test_new_exact_head_strong_findings_dispatch_while_historical_closure_is_pending(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow = make_flow(flow_env, monkeypatch, 7104, "local", saved_status="BLOCKED")
    flow.accept_finding(finding_id="historical-gap")
    snapshot = flow_env.ledger.get_snapshot("https://api.github.com", REPO, flow.pr)
    h2 = _repair_to(flow, "new-audit-head")
    flow_env.ledger.record_transition(
        "https://api.github.com",
        REPO,
        flow.pr,
        "local-verification",
        snapshot.ledger_revision,
        snapshot.blockers[0].blocker_id,
        BlockerDisposition.VERIFIED_CORRECTION,
        "Independent local verifier accepted the historical correction",
        reviewed_head_sha=h2,
    )
    flow_env.head = h2
    flow.root_id += 1
    flow.thread_id = "PRRT_current_finding"
    flow.accept_finding(gap=False, finding_id="current-defect")

    actions = flow.run(closure=ClosureScript())

    assert flow.handoffs == 1 and flow.model_calls == 0, actions
    retained = flow.retained()
    assert retained is not None and retained.status == "NEEDS_FIX"
    assert len(retained.source_identities) == 1 and retained.source_identities[0].endswith(":current-defect")
    assert "current-defect" in flow.handoff_text()
    assert "historical-gap" not in flow.handoff_text()
    assert {finding.finding_id for finding in flow_env.cycle.snapshot(flow.pr).open_findings} == {"historical-gap", "current-defect"}
    assert flow.merge.call_count == 0 and flow.reviews == []


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


@pytest.mark.parametrize("extension", ["missing", "malformed"])
@pytest.mark.parametrize("boundary", ["before-publication", "before-handoff"])
def test_current_correction_evidence_survives_interruption_without_closure_extension(flow_env: Env, monkeypatch: pytest.MonkeyPatch, extension: str, boundary: str) -> None:
    flow, script, _ = _historical_root_with_current_violation(flow_env, monkeypatch, "cloud", extension)
    assert flow.router is not None

    def crash(*_args: object, **_kwargs: object) -> None:
        raise SimulatedCrash

    if boundary == "before-publication":
        with patch("auto_coder.pr_processor._publish_effective_review", side_effect=crash), pytest.raises(SimulatedCrash):
            flow.run(closure=script)
        assert flow.reviews == []
    else:
        with patch("auto_coder.pr_processor._dispatch_effective_correction", side_effect=crash), pytest.raises(SimulatedCrash):
            flow.run(closure=script)
        assert len(flow.reviews) == 1
    retained = flow.retained()
    assert retained is not None and retained.ordinary_correction is not None
    assert retained.ordinary_correction.restore().findings[0].actual_behavior == "State is discarded"
    assert flow.handoffs == 0
    _reconstruct_flow_stores(flow)
    flow.model_responses = []  # The retained producer result is the only available corrective evidence.

    actions = flow.run(closure=script)
    again = flow.run(closure=script)

    assert flow.model_calls == 1 and flow.handoffs == 1 and len(flow.reviews) == 1, (list(actions), list(again))
    assert flow.merge.call_count == 0 and flow.env.cycle.snapshot(flow.pr).open_findings
    assert "Counterexample for finding-a" not in flow.handoff_text()


@pytest.mark.parametrize("changed_authority", ["base", "contract"])
def test_published_current_correction_rechecks_authority_before_handoff(flow_env: Env, monkeypatch: pytest.MonkeyPatch, changed_authority: str) -> None:
    from auto_coder.pr_review_cycle import ContractSnapshot

    flow, script, _ = _historical_root_with_current_violation(flow_env, monkeypatch, "cloud", "missing")
    assert flow.router is not None
    changes: list[str] = []

    def change_authority_during_publication() -> None:
        changes.append(changed_authority)
        if changed_authority == "base":
            flow.current_base = "base-advanced-during-publication"
        else:
            monkeypatch.setattr("tests.test_effective_decision_pr_flow.CONTRACT", ContractSnapshot(("#2401",), "Issue #2401 REQ-001: Preserve accepted findings under a newly revised contract."))

    flow.router.before_review = change_authority_during_publication
    actions = flow.run(closure=script, public_origin=True)

    retained = flow.retained()
    assert changes == [changed_authority]
    assert flow.model_calls == 1 and len(flow.reviews) == 1 and flow.handoffs == 0, actions
    assert retained is not None and retained.publication == "CONFIRMED" and retained.handoff != HANDOFF_DISPATCHED
    assert flow.status.outcome is PRProcessingOutcome.DEFERRED
    assert flow.merge.call_count == 0 and flow.env.cycle.snapshot(flow.pr).open_findings


def _seed_legacy_blocked_envelope(flow: Flow, *, retain_corrective_payload: bool) -> None:
    """Restore the persisted pre-upgrade envelope; keep authoritative producer evidence intact.

    The incident regression above exercises the original failure naturally. This
    fixture isolates upgrade recovery without monkeypatching decision derivation.
    """
    from auto_coder.effective_decision_application import HANDOFF_NOT_REQUIRED

    store = EffectiveDecisionStore(REPO)
    retained = store.load(flow.pr)
    assert retained is not None
    snapshot = flow.env.cycle.snapshot(flow.pr)
    strong = snapshot.accepted_strong_round
    assert strong is not None
    source = f"{strong.round_id}:finding-a"
    projection = flow.env.bridge().project(flow.env.target(flow.pr, flow.head))
    historical = next(record for record in projection.records if record.source_identity == source)
    store.save(
        replace(
            retained,
            status="BLOCKED",
            next_action="RECONCILIATION",
            blocker_ids=[historical.canonical_blocker_id],
            source_identities=[source],
            pending_closure_source_identities=[],
            pending_reconciliation_source_identities=[],
            ordinary_correction=retained.ordinary_correction if retain_corrective_payload else None,
            handoff=HANDOFF_NOT_REQUIRED,
            reconciliation_attempts=[retained.evidence_revision],
            wait_reason="RECONCILIATION_PENDING",
        )
    )
    assert flow.router is not None
    for review in [*flow.reviews, *flow.router.review_records]:
        if f"adversarial-validation:v11:{flow.head}" in review["body"]:
            review["body"] = review["body"].replace("adversarial validation: NEEDS_FIX", "adversarial validation: BLOCKED")
            review["event"] = "COMMENT"
            if "state" in review:
                review["state"] = "COMMENTED"
    assert flow._published_status() == "BLOCKED"


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("evidence_source", ["ordinary-payload", "legacy-closure-record", "legacy-closure-without-causal-alias"])
def test_saved_blocked_current_correction_replays_authoritative_evidence_after_restart(flow_env: Env, monkeypatch: pytest.MonkeyPatch, force: bool, evidence_source: str) -> None:
    flow, script, _ = _historical_root_with_current_violation(flow_env, monkeypatch, "cloud", "missing" if evidence_source == "ordinary-payload" else "complete")
    with patch("auto_coder.pr_processor._dispatch_effective_correction", side_effect=SimulatedCrash), pytest.raises(SimulatedCrash):
        flow.run(closure=script)
    assert flow.model_calls == 1 and flow.handoffs == 0 and len(flow.reviews) == 1
    _seed_legacy_blocked_envelope(flow, retain_corrective_payload=evidence_source == "ordinary-payload")
    if evidence_source == "legacy-closure-without-causal-alias":
        from auto_coder.ordinary_correction_references import ORDINARY_OBSERVATION_ALIAS

        # Model a pre-upgrade ledger without the newly introduced causal alias;
        # retain its accepted owners, native roots, scope and delivery history.
        with flow.env.ledger._connect() as connection:
            removed = connection.execute("DELETE FROM blocker_aliases WHERE alias_type = ?", (ORDINARY_OBSERVATION_ALIAS,)).rowcount
        assert removed == 1
    owners = [(blocker.blocker_id, blocker.accepted_scope) for blocker in flow.env.ledger.get_snapshot("https://api.github.com", REPO, flow.pr).blockers]
    assert flow.router is not None
    root_ids = {root["id"] for roots in flow.router.review_roots.values() for root in roots}
    _reconstruct_flow_stores(flow)
    flow.model_responses = []

    actions = flow.run(force=force, closure=script, public_origin=True)
    first_public_status = flow.status
    again = flow.run(force=force, closure=script, public_origin=True)

    assert flow.model_calls == 1 and flow.handoffs == 1 and len(flow.reviews) == 1, (list(actions), list(again))
    assert first_public_status.pr_data["number"] == flow.pr and flow.status.pr_data["number"] == flow.pr
    assert [(blocker.blocker_id, blocker.accepted_scope) for blocker in flow.env.ledger.get_snapshot("https://api.github.com", REPO, flow.pr).blockers] == owners
    assert {root["id"] for roots in flow.router.review_roots.values() for root in roots} == root_ids
    assert flow.merge.call_count == 0 and flow.env.cycle.snapshot(flow.pr).open_findings
    assert flow.retained().ordinary_correction is not None
    assert "State is discarded" in flow.handoff_text() and "Counterexample for finding-a" not in flow.handoff_text()


@pytest.mark.parametrize("thread_gate_enabled", [True, False], ids=["early-unresolved-thread-route", "ordinary-saved-result-route"])
def test_public_saved_review_routes_resume_real_durable_correction_without_new_reviewer(flow_env: Env, monkeypatch: pytest.MonkeyPatch, thread_gate_enabled: bool) -> None:
    """REQ-009: both public routes enter the real service and reuse durable attempt/effect identity."""
    from auto_coder.pr_review_resumption import SavedReviewResumptionService

    flow, script, _ = _historical_root_with_current_violation(flow_env, monkeypatch, "cloud", "missing")
    with patch("auto_coder.pr_processor._dispatch_effective_correction", side_effect=SimulatedCrash), pytest.raises(SimulatedCrash):
        flow.run(closure=script)
    retained_before = flow.retained()
    assert retained_before is not None and retained_before.ordinary_correction is not None
    producing_attempt = retained_before.ordinary_correction.attempt_id
    source_identities = tuple(retained_before.source_identities)
    _reconstruct_flow_stores(flow)
    flow.model_responses = []
    service_calls: list[int] = []
    real_resume = SavedReviewResumptionService.resume_before_admission

    def observe_resume(service, request):
        service_calls.append(int(request.pr_data["number"]))
        return real_resume(service, request)

    monkeypatch.setattr(SavedReviewResumptionService, "resume_before_admission", observe_resume)
    actions = flow.run(closure=script, public_origin=True, thread_gate_enabled=thread_gate_enabled)
    retained_after_first = flow.retained()
    assert retained_after_first is not None
    blocker_ids = tuple(retained_after_first.blocker_ids)
    again = flow.run(closure=script, public_origin=True, thread_gate_enabled=thread_gate_enabled)

    retained_after = flow.retained()
    assert service_calls == [flow.pr, flow.pr]
    assert flow.model_calls == 1 and flow.handoffs == 1 and len(flow.reviews) == 1, (actions, again)
    assert retained_after is not None and retained_after.ordinary_correction is not None
    assert retained_after.ordinary_correction.attempt_id == producing_attempt
    assert tuple(retained_after.blocker_ids) == blocker_ids and blocker_ids
    assert tuple(retained_after.source_identities) == source_identities
    assert retained_after.handoff == HANDOFF_DISPATCHED
    assert flow.status.pr_data["number"] == flow.pr and flow.merge.call_count == 0


@pytest.mark.parametrize("newer_status", ["IN_PROGRESS", "ERROR"])
def test_superseded_saved_correction_cannot_dispatch_older_evidence(flow_env: Env, monkeypatch: pytest.MonkeyPatch, newer_status: str) -> None:
    flow, script, _ = _historical_root_with_current_violation(flow_env, monkeypatch, "cloud", "missing")
    with patch("auto_coder.pr_processor._dispatch_effective_correction", side_effect=SimulatedCrash), pytest.raises(SimulatedCrash):
        flow.run(closure=script)
    _seed_legacy_blocked_envelope(flow, retain_corrective_payload=True)
    attempts = AdversarialValidationAttemptRepository(REPO)
    newer = attempts.start(flow.pr, flow.head)
    if newer_status == "ERROR":
        attempts.finish(newer.attempt_id, newer_status)
    _reconstruct_flow_stores(flow)
    flow.model_responses = []

    actions = flow.run(closure=script, public_origin=True)

    assert flow.handoffs == 0 and len(flow.reviews) == 1 and flow.model_calls == 1, actions
    assert flow.merge.call_count == 0 and flow.env.cycle.snapshot(flow.pr).open_findings
    assert flow.status.outcome in {PRProcessingOutcome.DEFERRED, PRProcessingOutcome.FAILED}


def test_explicit_force_after_a_newer_failed_attempt_admits_fresh_corrective_evidence(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow, script, raw = _historical_root_with_current_violation(flow_env, monkeypatch, "cloud", "missing")
    with patch("auto_coder.pr_processor._dispatch_effective_correction", side_effect=SimulatedCrash), pytest.raises(SimulatedCrash):
        flow.run(closure=script)
    _seed_legacy_blocked_envelope(flow, retain_corrective_payload=True)
    attempts = AdversarialValidationAttemptRepository(REPO)
    newer = attempts.start(flow.pr, flow.head)
    attempts.finish(newer.attempt_id, "ERROR")
    raw["thread_dispositions"] = [{"thread_id": thread.id, "status": "STILL_VALID", "rationale": "The distinct correction remains required.", "evidence": "src/state.py:40 still drops state."} for thread in flow.threads() if not thread.is_resolved]
    flow.model_responses = [json.dumps(raw)]
    _reconstruct_flow_stores(flow)

    actions = flow.run(force=True, closure=script)
    _reconstruct_flow_stores(flow)
    flow.model_responses = []
    again = flow.run(closure=script)

    retained = flow.retained()
    assert flow.model_calls == 2 and flow.handoffs == 1 and len(flow.reviews) == 2, (list(actions), list(again))
    assert retained is not None and retained.ordinary_correction is not None
    assert retained.ordinary_correction.attempt_sequence > newer.sequence
    assert retained.handoff == HANDOFF_DISPATCHED
    assert flow.merge.call_count == 0 and flow.env.cycle.snapshot(flow.pr).open_findings


def test_legacy_blocked_without_any_ordinary_payload_admits_one_recovery_then_retains_it(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow, script, raw = _historical_root_with_current_violation(flow_env, monkeypatch, "cloud", "missing")
    with patch("auto_coder.pr_processor._dispatch_effective_correction", side_effect=SimulatedCrash), pytest.raises(SimulatedCrash):
        flow.run(closure=script)
    _seed_legacy_blocked_envelope(flow, retain_corrective_payload=False)
    raw["thread_dispositions"] = [{"thread_id": thread.id, "status": "STILL_VALID", "rationale": "The distinct current correction remains required.", "evidence": "src/state.py:40 still drops state."} for thread in flow.threads() if not thread.is_resolved]
    flow.model_responses = [json.dumps(raw)]
    _reconstruct_flow_stores(flow)
    admissions: list[str] = []

    def assert_recovery_was_spent_before_review() -> None:
        record = flow.retained()
        assert record is not None
        keys = [key for key in record.reconciliation_attempts if key.startswith("ordinary-correction-v1:")]
        assert len(keys) == 1
        admissions.extend(keys)

    script.on_review = assert_recovery_was_spent_before_review

    actions = flow.run(closure=script)
    _reconstruct_flow_stores(flow)
    flow.model_responses = []
    again = flow.run(closure=script)

    assert flow.model_calls == 2 and flow.handoffs == 1, (list(actions), list(again))
    assert len(admissions) == 1
    assert len(flow.reviews) == 2
    retained = flow.retained()
    assert retained is not None and retained.ordinary_correction is not None and retained.handoff == HANDOFF_DISPATCHED
    assert flow.env.cycle.snapshot(flow.pr).open_findings and flow.merge.call_count == 0


def test_interrupted_legacy_recovery_cannot_obtain_another_unchanged_admission(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow, script, _ = _historical_root_with_current_violation(flow_env, monkeypatch, "cloud", "missing")
    with patch("auto_coder.pr_processor._dispatch_effective_correction", side_effect=SimulatedCrash), pytest.raises(SimulatedCrash):
        flow.run(closure=script)
    _seed_legacy_blocked_envelope(flow, retain_corrective_payload=False)

    def interrupted_reviewer() -> None:
        retained = flow.retained()
        assert retained is not None and any(key.startswith("ordinary-correction-v1:") for key in retained.reconciliation_attempts)
        raise SimulatedCrash

    script.on_review = interrupted_reviewer
    with pytest.raises(SimulatedCrash):
        flow.run(closure=script)
    _reconstruct_flow_stores(flow)
    flow.model_responses = []

    first = flow.run(closure=script)
    second = flow.run(closure=script)

    assert flow.model_calls == 2 and flow.handoffs == 0 and len(flow.reviews) == 1, (list(first), list(second))
    assert first.disposition.value == "RECONCILIATION_WAIT" and second.disposition.value == "RECONCILIATION_WAIT"
    assert flow.merge.call_count == 0 and flow.env.cycle.snapshot(flow.pr).open_findings


def test_current_correction_lost_provider_response_reconciles_after_restart(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow, script, _ = _historical_root_with_current_violation(flow_env, monkeypatch, "cloud", "malformed")
    flow.provider.lose_next_response = True

    first = flow.run(closure=script, public_origin=True)

    assert flow.status.outcome is PRProcessingOutcome.DEFERRED and flow.retained().handoff == HANDOFF_WAITING
    assert len(flow.provider.followups) == 1
    _reconstruct_flow_stores(flow)
    flow.model_responses = []
    second = flow.run(closure=script, public_origin=True)
    third = flow.run(closure=script, public_origin=True)
    assert any("already delivered" in action for action in third), (second, third)
    assert flow.model_calls == 1 and len(flow.provider.followups) == 1 and len(flow.reviews) == 1
    assert flow.retained().handoff == HANDOFF_DISPATCHED and flow.merge.call_count == 0


def test_current_correction_accepted_review_with_lost_response_uses_confirmed_roots(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    class AcceptedResponseLostClient(RouterClient):
        lose_next_response = True

        def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
            response = super().request(method, url, **kwargs)
            if method == "POST" and url.endswith("/reviews") and self.lose_next_response:
                self.lose_next_response = False
                raise httpx.ConnectError("accepted response lost", request=response.request)
            return response

    flow, script, _ = _historical_root_with_current_violation(flow_env, monkeypatch, "cloud", "missing")
    assert flow.router is not None
    transport = AcceptedResponseLostClient(flow.head)
    transport.__dict__.update(flow.router.__dict__)
    flow.router = transport

    actions = flow.run(closure=script, public_origin=True)
    _reconstruct_flow_stores(flow)
    flow.model_responses = []
    again = flow.run(closure=script, public_origin=True)

    assert flow.model_calls == 1 and flow.handoffs == 1 and len(flow.reviews) == 1, (list(actions), list(again))
    assert len(transport.review_records) == 2  # Initial Strong review and the accepted ordinary review.
    assert len(transport.review_roots[max(transport.review_roots)]) == 1
    assert flow.retained().publication == "CONFIRMED"
    assert flow.merge.call_count == 0 and flow.env.cycle.snapshot(flow.pr).open_findings


def test_current_correction_uncertain_publication_waits_for_receipt_without_resubmission(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow, script, _ = _historical_root_with_current_violation(flow_env, monkeypatch, "cloud", "missing")
    assert flow.router is not None

    def lose_process_after_transmission_is_journaled() -> None:
        raise SimulatedCrash

    flow.router.before_review = lose_process_after_transmission_is_journaled
    with pytest.raises(SimulatedCrash):
        flow.run(closure=script)
    # The durable intent cannot know whether a terminated transport sent its request.
    flow.router.before_review = lambda: None
    _reconstruct_flow_stores(flow)
    flow.model_responses = []

    first = flow.run(closure=script)
    second = flow.run(closure=script)

    assert flow.model_calls == 1 and flow.reviews == [] and flow.handoffs == 0
    assert any("publication" in action.lower() and "unresolved" in action.lower() for action in first), first
    assert any("publication" in action.lower() and "unresolved" in action.lower() for action in second), second
    assert first.disposition.value == second.disposition.value == "CORRECTION_WAITING"
    retained = flow.retained()
    assert retained is not None and retained.publication == "PENDING" and retained.ordinary_correction is not None
    assert flow.status.outcome is PRProcessingOutcome.DEFERRED and flow.merge.call_count == 0


@pytest.mark.parametrize(("gate", "wait_reason"), [("origin_available", "ROUTE_UNAVAILABLE"), ("quota_hold", "QUOTA_DEFERRED"), ("exhausted", "ALLOWANCE_EXHAUSTED")])
def test_current_correction_wait_keeps_ordinary_evidence_and_historical_obligation(flow_env: Env, monkeypatch: pytest.MonkeyPatch, gate: str, wait_reason: str) -> None:
    flow, script, _ = _historical_root_with_current_violation(flow_env, monkeypatch, "cloud", "missing")
    setattr(flow, gate, gate != "origin_available")

    actions = flow.run(closure=script)

    retained = flow.retained()
    assert retained is not None and retained.ordinary_correction is not None
    assert retained.handoff == HANDOFF_WAITING and retained.wait_reason == wait_reason, actions
    assert actions.disposition.value == "CORRECTION_WAITING"
    assert flow.handoffs == 0 and flow.merge.call_count == 0 and flow.env.cycle.snapshot(flow.pr).open_findings
    _reconstruct_flow_stores(flow)
    flow.model_responses = []
    again = flow.run(closure=script)
    assert flow.model_calls == 1 and len(flow.reviews) == 1 and flow.handoffs == 0, again


def test_current_correction_failed_durable_evidence_write_emits_no_effect(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow, script, _ = _historical_root_with_current_violation(flow_env, monkeypatch, "cloud", "missing")

    with patch.object(EffectiveDecisionStore, "_write", side_effect=OSError("disk full")):
        actions = flow.run(closure=script, public_origin=True)

    assert flow.model_calls == 1 and flow.reviews == [] and flow.handoffs == 0
    assert flow.status.outcome is PRProcessingOutcome.FAILED
    assert flow.merge.call_count == 0 and flow.env.cycle.snapshot(flow.pr).open_findings


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
    assert first.disposition.value == "CORRECTION_WAITING"
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

    with monkeypatch.context() as failing_write:
        failing_write.setattr(EffectiveDecisionStore, "_write", fail_write)
        actions = flow.run()

    assert flow.reviews == [] and flow.handoffs == 0 and flow.merge.call_count == 0, actions
    assert any("could not be retained" in action and "no dependent review effect was emitted" in action for action in actions)
    assert flow.status.outcome is PRProcessingOutcome.FAILED
    assert actions.disposition.value == "OPERATIONAL_FAILURE"
    assert flow.retained() is None


# -- AS-007 -----------------------------------------------------------------


def test_unavailable_origin_route_is_a_distinct_wait_and_the_next_run_consumes_the_pending_work(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow = _unfinished_flow(flow_env, monkeypatch, 9100, origin_available=False)

    actions = flow.run()

    assert [review["event"] for review in flow.reviews] == ["REQUEST_CHANGES"] and flow.handoffs == 0, actions
    retained = flow.retained()
    assert retained is not None and retained.handoff == HANDOFF_WAITING and retained.wait_reason == WAIT_ROUTE_UNAVAILABLE
    assert flow.status.outcome is not PRProcessingOutcome.SUCCESS and flow.merge.call_count == 0
    assert actions.disposition.value == "CORRECTION_WAITING"
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
    actions = flow.run()
    assert flow.model_calls == 2, "\n".join(actions)  # eligible again on relevant new evidence
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
    flow.run(public_origin=True)
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
    flow.run(closure=outage, public_origin=True)
    assert len(outage.calls) == 1 and flow.model_calls == calls_before + 1
    assert flow_env.cycle.snapshot(flow.pr).accepted_closure is None  # retained, not applied: an unavailable read never certifies
    assert [item.finding_id for item in flow_env.cycle.snapshot(flow.pr).open_findings] == ["finding-a"]

    flow.auto_status = True  # GitHub now holds the saved review for this head
    restored = ClosureScript(status="FIXED")
    calls_before = flow.model_calls
    actions = flow.run(closure=restored, public_origin=True)  # normal processing, same head, no commit, no store reset

    # The retained assessment is applied from the durable source: no closure-only
    # reviewer runs and the same evidence is never re-requested with closure context.
    assert restored.calls == [] and restored.closure_only_calls == [], actions
    assert flow.model_calls == calls_before, "\n".join(actions)
    assert flow.handoffs == 1, "\n".join(actions)
    snapshot = flow_env.cycle.snapshot(flow.pr)
    assert snapshot.accepted_closure is not None and snapshot.open_findings == (), "\n".join(actions)
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
