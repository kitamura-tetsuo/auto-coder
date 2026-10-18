"""Bounded provider-adapter composition for ordinary Issue dispatch."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, List, Mapping, Optional

from .automation_config import AutomationConfig
from .implementation_slots import ImplementationSlotRepository
from .issue_dispatch import AdapterOutcome, CandidateHandoff, DispatchOutcome, DispatchResult, IssueAttemptIdentity, IssueDispatchGuard
from .issue_stage_routing import ImplementationRetryRequest
from .label_manager import LabelManagerContext, filter_legacy_auto_coder_label
from .util.gh_cache import GitHubClient

if TYPE_CHECKING:
    from .local_job_handoff import LocalJobStore


@dataclass(frozen=True)
class IssueDispatchExecution:
    """Structured ordinary-dispatch result plus its presentation actions."""

    result: DispatchResult
    actions: List[str]


@dataclass(frozen=True)
class OrdinaryIssueAdapterEffects:
    """Narrow ports to the provider-specific effect owners."""

    process_codex_cloud: Callable[..., List[str]]
    process_claude_routine: Callable[..., List[str]]
    process_jules: Callable[..., List[str]]
    take_local_actions: Callable[..., List[str]]
    record_dispatch_stage: Callable[..., None]


@dataclass(frozen=True)
class OrdinaryIssueAdapterInput:
    """The bounded inputs needed to compose existing ordinary provider adapters."""

    repo_name: str
    issue_data: Mapping[str, Any]
    config: AutomationConfig
    github_client: GitHubClient
    candidate_names: List[str]
    identity: IssueAttemptIdentity
    numeric_attempt: int
    effects: OrdinaryIssueAdapterEffects
    authorize_new_attempt: bool = False
    label_context: Optional[LabelManagerContext] = None
    implementation_slots: Optional[ImplementationSlotRepository] = None
    retry_authority: Optional[ImplementationRetryRequest] = None
    local_job_store: Optional["LocalJobStore"] = None
    implementation_execution_id: str = ""


class OrdinaryIssueAdapterComposition:
    """Resolve candidates and invoke the existing provider effect owners."""

    _REMOTE_TYPES = {"codex-cloud", "claude-routine", "jules"}
    _LOCAL_TYPES = {"codex", "codex-mcp", "antigravity", "qwen", "auggie", "muse", "claude", "aider", "opencode"}

    def __init__(self, dispatch_guard: Optional[IssueDispatchGuard] = None) -> None:
        self._dispatch_guard = dispatch_guard or IssueDispatchGuard()

    def dispatch(self, request: OrdinaryIssueAdapterInput) -> IssueDispatchExecution:
        from .cli_helpers import build_backend_manager
        from .cloud_manager import CloudManager
        from .cloud_run import CloudRunRepository
        from .exceptions import AutoCoderUsageLimitError, CloudSubmissionNotStartedError
        from .execution_trace import EventKind, Outcome
        from .git_info import get_commit_log
        from .llm_backend_config import get_llm_config
        from .prompt_loader import render_prompt
        from .retry_dispatch import RetryDispatchRepository
        from .worktree_utils import LocalPreparationNotStartedError

        issue_number = request.identity.issue_number
        llm_config = get_llm_config(repo_name=request.repo_name)
        candidates: List[CandidateHandoff] = []
        for name in request.candidate_names:
            backend_config = llm_config.get_backend_config(name)
            provider = (backend_config.backend_type if backend_config is not None else None) or name
            candidates.append(CandidateHandoff(name, provider, request.local_job_store is not None and provider.lower() in self._LOCAL_TYPES))

        actions: List[str] = []

        def invoke(candidate: CandidateHandoff) -> AdapterOutcome:
            nonlocal actions
            backend_type = candidate.provider.lower()
            observed_acceptance: List[AdapterOutcome] = []
            request.effects.record_dispatch_stage(
                issue_number,
                "issue.dispatch.selection",
                f"issue#{issue_number} dispatch candidate selected",
                Outcome.UNKNOWN,
                {"candidate_pool": "cloud" if backend_type in self._REMOTE_TYPES else "local", "backend_type": backend_type, "backend_name": candidate.backend_name},
                kind=EventKind.STAGE_STARTED,
            )
            try:
                if backend_type == "codex-cloud":
                    actions = request.effects.process_codex_cloud(
                        request.repo_name,
                        request.issue_data,
                        request.config,
                        request.github_client,
                        backend_name=candidate.backend_name,
                        label_context=request.label_context,
                        implementation_slots=request.implementation_slots,
                        retry_authority=request.retry_authority,
                    )
                elif backend_type == "claude-routine":
                    actions = request.effects.process_claude_routine(
                        request.repo_name,
                        request.issue_data,
                        request.config,
                        request.github_client,
                        backend_name=candidate.backend_name,
                        label_context=request.label_context,
                        acceptance_observer=observed_acceptance.append,
                        retry_authority=request.retry_authority,
                    )
                elif backend_type == "jules":
                    actions = request.effects.process_jules(
                        request.repo_name,
                        request.issue_data,
                        request.config,
                        request.github_client,
                        label_context=request.label_context,
                        implementation_slots=request.implementation_slots,
                        backend_name=candidate.backend_name,
                        acceptance_observer=observed_acceptance.append,
                        **({"retry_authority": request.retry_authority} if request.retry_authority is not None else {}),
                    )
                elif backend_type in self._LOCAL_TYPES:
                    model = llm_config.get_model_for_backend(candidate.backend_name) or ""
                    manager = build_backend_manager(
                        selected_backends=[candidate.backend_name],
                        primary_backend=candidate.backend_name,
                        models={candidate.backend_name: model},
                    )
                    if request.local_job_store is not None:
                        from .local_job_handoff import LocalJobKind, LocalJobOffer

                        issue_labels = filter_legacy_auto_coder_label(request.issue_data.get("labels", []))
                        prompt = render_prompt(
                            "issue.local_implementation",
                            repo_name=request.repo_name,
                            issue_number=issue_number,
                            issue_title=request.issue_data.get("title", "Unknown"),
                            issue_body=request.issue_data.get("body") or "",
                            issue_labels=", ".join(issue_labels),
                            issue_state=request.issue_data.get("state", "open"),
                            issue_author=(request.issue_data.get("user") or {}).get("login", request.issue_data.get("author", "unknown")),
                            commit_log=get_commit_log(base_branch=request.config.MAIN_BRANCH) or "(No commit history)",
                            parent_issue_body=request.issue_data.get("parent_issue_body", ""),
                            main_branch=request.config.MAIN_BRANCH,
                        )
                        accepted = request.local_job_store.offer_issue(
                            LocalJobOffer(
                                LocalJobKind.ISSUE_IMPLEMENTATION,
                                request.repo_name,
                                issue_number,
                                request.identity.implementation_attempt_id,
                                candidate.backend_name,
                                prompt,
                                request.implementation_execution_id,
                            ),
                            request.identity,
                            self._dispatch_guard,
                        )
                        if accepted is None:
                            return AdapterOutcome(DispatchOutcome.INDETERMINATE, diagnostic="durable local-job handoff was not confirmed", tracking_complete=False)
                        actions = [f"Accepted local implementation job {accepted.job_id} for issue #{issue_number}"]
                        if request.label_context is not None:
                            request.label_context.keep_label()
                        request.effects.record_dispatch_stage(
                            issue_number,
                            "issue.dispatch.local-job",
                            f"issue#{issue_number} local implementation handoff",
                            Outcome.ACCEPTED_HANDOFF,
                            {"backend_name": candidate.backend_name, "job_id": accepted.job_id, "state": accepted.state.value},
                        )
                        return AdapterOutcome(DispatchOutcome.LOCAL_ACCEPTED, accepted.job_id)
                    actions = request.effects.take_local_actions(
                        request.repo_name,
                        request.issue_data,
                        request.config,
                        request.github_client,
                        backend_manager=manager,
                        implementation_slots=request.implementation_slots,
                        implementation_attempt=request.identity.implementation_attempt_id,
                        raise_on_failure=True,
                        **({"retry_authority": request.retry_authority} if request.retry_authority is not None else {}),
                    )
                    return AdapterOutcome(DispatchOutcome.LOCAL_COMPLETED)
                else:
                    return AdapterOutcome(DispatchOutcome.NOT_STARTED, diagnostic=f"unsupported backend type: {backend_type}")
            except (AutoCoderUsageLimitError, CloudSubmissionNotStartedError, FileNotFoundError, LocalPreparationNotStartedError) as exc:
                return AdapterOutcome(DispatchOutcome.NOT_STARTED, diagnostic=str(exc))
            except Exception as exc:
                return AdapterOutcome(DispatchOutcome.INDETERMINATE, diagnostic=str(exc))

            if observed_acceptance:
                return observed_acceptance[-1]
            binding = CloudManager(request.repo_name).get_binding(issue_number)
            if binding is not None and binding.provider == backend_type and binding.backend_name == candidate.backend_name:
                return AdapterOutcome(DispatchOutcome.REMOTE_ACCEPTED, binding.task_id)
            if backend_type == "codex-cloud":
                match_attempt = request.numeric_attempt
                if request.retry_authority is not None:
                    retry_handoff = RetryDispatchRepository(request.repo_name).get(request.retry_authority.request_id)
                    if retry_handoff is not None and retry_handoff.numeric_attempt is not None:
                        match_attempt = retry_handoff.numeric_attempt
                runs = CloudRunRepository(request.repo_name).list_for_issue(issue_number)
                matching = [run for run in runs if str(run.attempt) == str(match_attempt) and run.backend_name == candidate.backend_name]
                if matching and matching[-1].task_id and matching[-1].submission_outcome == "accepted":
                    return AdapterOutcome(DispatchOutcome.REMOTE_ACCEPTED, matching[-1].task_id, "secondary tracking incomplete")
            assert backend_type in self._REMOTE_TYPES
            return AdapterOutcome(DispatchOutcome.INDETERMINATE, diagnostic="remote adapter returned without durable provider acceptance evidence")

        result = self._dispatch_guard.dispatch_candidates(request.identity, candidates, invoke, authorize_new_attempt=request.authorize_new_attempt)
        return IssueDispatchExecution(result, actions)
