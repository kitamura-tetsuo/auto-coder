"""Bounded execution boundary for PR base updates and conflict repairs.

The service deliberately owns no provider or Git implementation.  Those remain
with the existing producers, while this boundary carries the immutable target
identity and returns their typed facts to PR orchestration without interpreting
display actions.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, replace
from typing import Any, Callable

from .pr_repair_dispatch import PRRepairResult, RepairDisposition


@dataclass(frozen=True)
class BaseUpdateExecutionRequest:
    repository: str
    pr_number: int
    expected_head: str
    base_ref: str
    origin: str

    def __post_init__(self) -> None:
        if not self.repository or self.pr_number <= 0:
            raise ValueError("repository and positive PR number are required")


@dataclass(frozen=True)
class BaseUpdateExecutionResult:
    repair: PRRepairResult
    actions: tuple[str, ...]
    quota_deferred: bool = False
    retry_not_before: float | None = None


class BaseUpdateConflictExecutionService:
    """Compose already-selected base-update/conflict operation producers."""

    def execute_base_update(
        self,
        request: BaseUpdateExecutionRequest,
        pr_data: dict[str, Any] | Callable[[], BaseUpdateExecutionResult],
        config: Any = None,
        github_client: Any = None,
    ) -> BaseUpdateExecutionResult:
        if callable(pr_data):
            produced = pr_data()
        else:
            actions = _run_base_update_operation(request.repository, pr_data, config, github_client)
            repair = actions.repair_result or PRRepairResult(
                RepairDisposition.FAILED,
                tuple(actions),
                reason="base update returned no typed result",
            )
            produced = BaseUpdateExecutionResult(repair, tuple(actions), actions.quota_deferred, actions.retry_not_before)
        repair = self._bind_target(request, produced.repair, produced.actions)
        return replace(produced, repair=repair)

    def execute_conflict_repair(
        self,
        request: BaseUpdateExecutionRequest,
        operation: Any = None,
        *,
        config: Any = None,
    ) -> PRRepairResult:
        """Execute one route selected by the caller; never introduce fallback."""
        if config is not None:
            from . import pr_processor as p

            try:
                token = p.GitHubClient.get_instance().token
                api = p.get_ghapi_client(token)
                owner, repo = request.repository.split("/")
                current_pr = api.pulls.get(owner, repo, request.pr_number)
            except Exception as exc:
                produced = PRRepairResult(RepairDisposition.FAILED, reason=f"could not verify local conflict target: {exc}")
                return self._bind_target(request, produced, produced.actions)
            current_head = str(current_pr.get("head", {}).get("sha") or "")
            current_base = str(current_pr.get("base", {}).get("ref") or "")
            if current_head != request.expected_head or current_base != request.base_ref:
                produced = PRRepairResult(
                    RepairDisposition.REFUSED,
                    reason="PR head or configured base changed before local conflict execution",
                    repository=request.repository,
                    pr_number=request.pr_number,
                    input_head=current_head,
                    base_ref=current_base,
                )
                return produced
            applied = _run_local_conflict_operation(request, config, current_pr)
            head = p.cmd.run_command(["git", "rev-parse", "HEAD"])
            resulting_head = head.stdout.strip() if applied and head.success else ""
            produced = PRRepairResult(
                RepairDisposition.APPLIED if applied else RepairDisposition.FAILED,
                reason="" if applied else "local conflict repair did not synchronize a result",
                resulting_head_state=p.RepairFactState.AVAILABLE if resulting_head else p.RepairFactState.UNKNOWN,
                resulting_head=resulting_head,
                synchronization_state=p.RepairFactState.AVAILABLE,
                synchronized=applied,
            )
        else:
            if operation is None:
                raise ValueError("a selected conflict operation or local configuration is required")
            produced = operation()
        return self._bind_target(request, produced, produced.actions)

    @staticmethod
    def _bind_target(
        request: BaseUpdateExecutionRequest,
        repair: PRRepairResult,
        actions: tuple[str, ...],
    ) -> PRRepairResult:
        # A producer may enrich these facts, but it may not silently retarget an
        # operation selected for another repository, PR, base, or observed head.
        identities = (
            (repair.repository, request.repository, "repository"),
            (repair.pr_number, request.pr_number, "PR"),
            (repair.input_head, request.expected_head, "head"),
            (repair.base_ref, request.base_ref, "base"),
        )
        mismatch = next((name for actual, expected, name in identities if actual and expected and actual != expected), "")
        if mismatch:
            reason = f"base-update producer returned a mismatched {mismatch} identity"
            return PRRepairResult(
                RepairDisposition.FAILED,
                actions,
                reason=reason,
                repository=request.repository,
                pr_number=request.pr_number,
                input_head=request.expected_head,
                base_ref=request.base_ref,
            )
        return replace(
            repair,
            actions=actions,
            repository=request.repository,
            pr_number=request.pr_number,
            input_head=request.expected_head,
            base_ref=request.base_ref,
        )


def _run_base_update_operation(
    repo_name: str,
    pr_data: dict[str, Any],
    config: Any,
    github_client: Any | None = None,
) -> Any:
    """Update PR branch with latest base branch commits.

    This function merges the PR's base branch (e.g., main, develop) into the PR branch
    to bring it up to date before attempting fixes.
    """
    from . import pr_processor as p

    PRActionList = p.PRActionList
    RepairDisposition = p.RepairDisposition
    PRRepairResult = p.PRRepairResult
    RepairFactState = p.RepairFactState
    RepairKind = p.RepairKind
    cmd = p.cmd
    git_push = p.git_push
    logger = p.logger
    GitHubClient = p.GitHubClient
    _is_jules_pr = p._is_jules_pr
    _extract_session_id_from_pr_body = p._extract_session_id_from_pr_body
    _guard_outbound_jules_send = p._guard_outbound_jules_send
    render_prompt = p.render_prompt
    _dispatch_jules_repair = p._dispatch_jules_repair
    _is_dependabot_pr = p._is_dependabot_pr
    _dispatch_cloud_conflict_repair = p._dispatch_cloud_conflict_repair
    _is_local_llm_pr = p._is_local_llm_pr
    _dispatch_local_conflict_repair = p._dispatch_local_conflict_repair
    actions = PRActionList()
    pr_number = pr_data["number"]

    target_branch = pr_data.get("base_branch") or pr_data.get("base", {}).get("ref") or config.MAIN_BRANCH
    input_head = pr_data.get("head", {}).get("sha", "")

    def record(disposition: Any, *, reason: str = "", resulting_head: str = "", synchronized: bool | None = None) -> None:
        """Attach the authoritative control result without interpreting display text."""
        actions.repair_result = PRRepairResult(
            disposition,
            tuple(actions),
            reason=reason,
            resulting_head_state=RepairFactState.AVAILABLE if resulting_head else RepairFactState.UNKNOWN,
            resulting_head=resulting_head,
            synchronization_state=RepairFactState.AVAILABLE if synchronized is not None else RepairFactState.UNKNOWN,
            synchronized=synchronized is True,
            repository=repo_name,
            pr_number=pr_number,
            input_head=input_head,
            base_ref=target_branch,
        )

    try:
        # Determine target base branch for this PR
        # Fetch latest changes from origin
        result = cmd.run_command(["git", "fetch", "origin"])
        if not result.success:
            actions.append(f"Failed to fetch latest changes: {result.stderr}")
            record(RepairDisposition.FAILED, reason=actions[-1])
            return actions

        # Check if base branch has new commits
        result = cmd.run_command(["git", "rev-list", "--count", f"HEAD..refs/remotes/origin/{target_branch}"])
        if not result.success:
            actions.append(f"Failed to check {target_branch} branch status: {result.stderr}")
            record(RepairDisposition.FAILED, reason=actions[-1])
            return actions

        commits_behind = int(result.stdout.strip())
        if commits_behind == 0:
            actions.append(f"PR #{pr_number} is up to date with {target_branch} branch")
            head = cmd.run_command(["git", "rev-parse", "HEAD"])
            if not head.success or not head.stdout.strip():
                record(RepairDisposition.FAILED, reason="Failed to observe HEAD after base comparison")
            else:
                record(RepairDisposition.NO_UPDATE_NEEDED, resulting_head=head.stdout.strip())
            return actions

        actions.append(f"PR #{pr_number} is {commits_behind} commits behind {target_branch}, updating...")

        # Try to merge base branch
        result = cmd.run_command(["git", "merge", f"refs/remotes/origin/{target_branch}"])
        if result.success:
            actions.append(f"Successfully merged {target_branch} branch into PR #{pr_number}")

            # Push the updated branch using centralized helper with retry
            push_result = git_push()
            if push_result.success:
                actions.append(f"Pushed updated branch for PR #{pr_number}")
                # Signal to skip further LLM analysis for this PR in this run
                actions.append("ACTION_FLAG:SKIP_ANALYSIS")
                head = cmd.run_command(["git", "rev-parse", "HEAD"])
                record(RepairDisposition.APPLIED, resulting_head=head.stdout.strip() if head.success else "", synchronized=True)
            else:
                # Push failed - try one more time after a brief pause
                logger.warning(f"First push attempt failed: {push_result.stderr}, retrying...")
                time.sleep(2)
                retry_push_result = git_push()
                if retry_push_result.success:
                    actions.append(f"Pushed updated branch for PR #{pr_number} (after retry)")
                    actions.append("ACTION_FLAG:SKIP_ANALYSIS")
                    head = cmd.run_command(["git", "rev-parse", "HEAD"])
                    record(RepairDisposition.APPLIED, resulting_head=head.stdout.strip() if head.success else "", synchronized=True)
                else:
                    logger.error(f"Failed to push updated branch after retry: {retry_push_result.stderr}")
                    logger.error("Exiting application due to git push failure")
                    sys.exit(1)
        else:
            # Merge conflict occurred, check if Jules PR
            actions.append(f"Merge conflict detected for PR #{pr_number}")

            if _is_jules_pr(pr_data):
                actions.append(f"PR #{pr_number} is a Jules PR with merge conflicts. Requesting Jules to resolve it.")
                try:
                    from auto_coder.jules_client import JulesClient

                    jules_client = JulesClient()
                    session_id = _extract_session_id_from_pr_body(pr_data.get("body", ""))
                    if session_id:

                        def send_jules_conflict() -> Any:
                            # Keep the existing retirement admission immediately
                            # before the provider mutation.
                            if not _guard_outbound_jules_send(repo_name, config, session_id):
                                reason = f"Blocked merge-conflict resolution request for PR #{pr_number}: session '{session_id}' belongs to a durably retired implementation slot (REQ-009)"
                                return PRRepairResult(RepairDisposition.REFUSED, (reason,), reason=reason)
                            prompt = render_prompt("pr.jules_merge_conflict_resolution")
                            jules_client.send_message(session_id, prompt)
                            return PRRepairResult(
                                RepairDisposition.ACCEPTED_OR_UNCERTAIN,
                                (f"Requested Jules to resolve merge conflict in session {session_id}",),
                                attempt_started=True,
                            )

                        jules_repair = _dispatch_jules_repair(repo_name, pr_data, RepairKind.MERGE_CONFLICT, session_id, send_jules_conflict)
                        actions.extend(jules_repair.actions)
                        actions.repair_result = jules_repair
                        # We return here so we don't proceed with LLM fixing this PR right now
                        actions.append("ACTION_FLAG:SKIP_ANALYSIS")
                        return actions
                    else:
                        actions.append(f"Jules PR #{pr_number} has merge conflicts but no session ID found. Cannot delegate.")
                except Exception as e:
                    actions.append(f"Error requesting Jules to resolve conflict: {e}")

            # Dependency-bot PRs (Dependabot/Renovate) are never conflict-resolved:
            # the bot recreates the PR against the updated base branch by itself.
            if _is_dependabot_pr(pr_data):
                actions.append(f"PR #{pr_number} is a dependency-bot PR with merge conflicts. Skipping conflict resolution.")
                cmd.run_command(["git", "merge", "--abort"])
                actions.append("ACTION_FLAG:SKIP_ANALYSIS")
                record(RepairDisposition.REFUSED, reason=actions[-2])
                return actions

            reporting_client = github_client
            if reporting_client is None:
                try:
                    reporting_client = GitHubClient.get_instance()
                except Exception as exc:
                    logger.warning(f"Could not initialize GitHub reporting for PR #{pr_number}: {exc}")
            cloud_repair = _dispatch_cloud_conflict_repair(repo_name, pr_data, reporting_client)
            if cloud_repair.disposition is RepairDisposition.DEFERRED:
                actions.append(f"DEFERRED Claude merge-conflict repair for PR #{pr_number}: {cloud_repair.reason}")
                actions.quota_deferred = True
                actions.retry_not_before = cloud_repair.retry_not_before
                actions.repair_result = cloud_repair
                cmd.run_command(["git", "merge", "--abort"])
                actions.append("ACTION_FLAG:SKIP_ANALYSIS")
                return actions
            if cloud_repair.disposition is RepairDisposition.ACCEPTED_OR_UNCERTAIN:
                actions.repair_result = cloud_repair
                cmd.run_command(["git", "merge", "--abort"])
                actions.extend(cloud_repair.actions)
                actions.append(f"Delegated merge-conflict repair for PR #{pr_number} to its existing cloud session")
                actions.append("ACTION_FLAG:SKIP_ANALYSIS")
                return actions

            if not _is_local_llm_pr(pr_data):
                actions.repair_result = cloud_repair
                actions.append(f"Cloud merge-conflict repair could not be delegated for PR #{pr_number}: " f"{cloud_repair.reason}; deferring conflict resolution.")
                cmd.run_command(["git", "merge", "--abort"])
                actions.append("ACTION_FLAG:SKIP_ANALYSIS")
                return actions

            # Use the common subroutine for conflict resolution
            from .conflict_resolver import _perform_base_branch_merge_and_conflict_resolution, scan_conflict_markers

            local_conflict_repair = _dispatch_local_conflict_repair(
                repo_name,
                pr_data,
                lambda: _perform_base_branch_merge_and_conflict_resolution(
                    pr_number,
                    target_branch,
                    config,
                    pr_data,
                    repo_name,
                ),
            )
            actions.repair_result = local_conflict_repair

            if local_conflict_repair.disposition is RepairDisposition.APPLIED:
                actions.append(f"Successfully resolved merge conflicts for PR #{pr_number}")
                actions.append(f"Pushed updated branch for PR #{pr_number}")
                actions.append("ACTION_FLAG:SKIP_ANALYSIS")
            else:
                # Unresolved local conflicts are repair failures, not evidence
                # that a read-only quality check rejected the merge.
                remaining_conflicts = scan_conflict_markers()
                if remaining_conflicts and not _is_local_llm_pr(pr_data):
                    actions.append(f"LLM determined merge would degrade code quality for PR #{pr_number}, skipping merge attempt")
                    actions.append("ACTION_FLAG:DEGRADING_MERGE_SKIP_MERGE")
                else:
                    actions.append(f"Failed to resolve merge conflicts for PR #{pr_number}")

            # Preserve the provider/local operation facts while adding operation identity.
            if actions.repair_result is not None:
                actions.repair_result = replace(
                    actions.repair_result,
                    actions=tuple(actions),
                    repository=repo_name,
                    pr_number=pr_number,
                    input_head=input_head,
                    base_ref=target_branch,
                )

    except Exception as e:
        actions.append(f"Error updating with base branch for PR #{pr_number}: {e}")
        record(RepairDisposition.FAILED, reason=actions[-1])
    finally:
        if actions.repair_result is not None:
            actions.repair_result = replace(
                actions.repair_result,
                actions=tuple(actions),
                repository=repo_name,
                pr_number=pr_number,
                input_head=input_head,
                base_ref=target_branch,
            )

    return actions


def _run_local_conflict_operation(request: BaseUpdateExecutionRequest, config: Any, pr_data: dict[str, Any]) -> bool:
    """Resolve merge conflicts for a PR by checking it out and merging with its base branch (not necessarily main)."""
    from . import pr_processor as p

    cmd = p.cmd
    logger = p.logger
    _is_dependabot_pr = p._is_dependabot_pr
    _checkout_pr_branch = p._checkout_pr_branch
    git_push = p.git_push
    _get_merge_conflict_info = p._get_merge_conflict_info
    resolve_merge_conflicts_with_llm = p.resolve_merge_conflicts_with_llm
    ConflictPublicationDisposition = p.ConflictPublicationDisposition
    get_trace_logger = p.get_trace_logger
    repo_name = request.repository
    pr_number = request.pr_number
    base_branch = request.base_ref
    try:
        # Step 0: Clean up any existing git state
        logger.info(f"Cleaning up git state before resolving conflicts for PR #{pr_number}")

        # Reset any uncommitted changes
        reset_result = cmd.run_command(["git", "reset", "--hard"])
        if not reset_result.success:
            logger.warning(f"Failed to reset git state: {reset_result.stderr}")

        # Clean untracked files
        clean_result = cmd.run_command(["git", "clean", "-fd"])
        if not clean_result.success:
            logger.warning(f"Failed to clean untracked files: {clean_result.stderr}")

        # Abort any ongoing merge
        abort_result = cmd.run_command(["git", "merge", "--abort"])
        if abort_result.success:
            logger.info("Aborted ongoing merge")

        # Dependency-bot PRs (Dependabot/Renovate) are never conflict-resolved:
        # the bot recreates the PR against the updated base branch by itself.
        if _is_dependabot_pr(pr_data):
            logger.info(f"Skipping merge conflict resolution for dependency-bot PR #{pr_number}")
            return False

        # Step 2: Checkout the PR branch
        logger.info(f"Checking out PR #{pr_number} to resolve merge conflicts")
        # Use reusable _checkout_pr_branch which uses direct git commands
        checkout_success = _checkout_pr_branch(repo_name, {**(pr_data or {}), "number": pr_number}, config)

        if not checkout_success:
            logger.error(f"Failed to checkout PR #{pr_number}")
            return False

        # Step 3: Fetch the latest base branch
        logger.info(f"Fetching latest {base_branch} branch")
        fetch_result = cmd.run_command(["git", "fetch", "origin", base_branch])

        if not fetch_result.success:
            logger.error(f"Failed to fetch {base_branch} branch: {fetch_result.stderr}")
            return False

        # Step 4: Attempt to merge base branch
        logger.info(f"Merging refs/remotes/origin/{base_branch} into PR #{pr_number}")
        merge_result = cmd.run_command(["git", "merge", f"refs/remotes/origin/{base_branch}"])

        if merge_result.success:
            # No conflicts, push the updated branch using centralized helper with retry
            logger.info(f"Successfully merged {base_branch} into PR #{pr_number}, pushing changes")
            push_result = git_push()

            if push_result.success:
                logger.info(f"Successfully pushed updated branch for PR #{pr_number}")
                return True
            else:
                # Push failed - try one more time after a brief pause
                logger.warning(f"First push attempt failed: {push_result.stderr}, retrying...")
                time.sleep(2)
                retry_push_result = git_push()
                if retry_push_result.success:
                    logger.info(f"Successfully pushed updated branch for PR #{pr_number} (after retry)")
                    return True
                else:
                    logger.error(f"Failed to push updated branch after retry: {retry_push_result.stderr}")
                    return False
        else:
            # Merge conflicts detected, use LLM to resolve them
            logger.info(f"Merge conflicts detected for PR #{pr_number}, using LLM to resolve")

            # Get conflict information
            conflict_info = _get_merge_conflict_info()

            # Use LLM to resolve conflicts
            resolve_actions = resolve_merge_conflicts_with_llm(
                {**(pr_data or {}), "number": pr_number, "base_branch": base_branch},
                conflict_info,
                config,
                repo_name,
            )

            # Log the resolution actions
            for action in resolve_actions:
                logger.info(f"Conflict resolution action: {action}")

            # Check if conflicts were resolved successfully
            status_result = cmd.run_command(["git", "status", "--porcelain"])

            if resolve_actions.disposition is ConflictPublicationDisposition.APPLIED and status_result.success and not status_result.stdout.strip():
                logger.info(f"Merge conflicts resolved for PR #{pr_number}")
                get_trace_logger().log("Conflict Resolution", f"Resolved merge conflicts for PR #{pr_number}", item_type="pr", item_number=pr_number)
                return True
            else:
                logger.error(f"Failed to resolve merge conflicts for PR #{pr_number}")
                return False

    except Exception as e:
        logger.error(f"Error resolving merge conflicts for PR #{pr_number}: {e}")
        return False
