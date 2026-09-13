"""Adversarial validation functionality for Auto-Coder automation engine.

Validates PR implementation against the original issue specification / acceptance criteria
using a strong model to catch false-success PRs before merge.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Optional, Sequence

from .automation_config import AutomationConfig
from .backend_manager import BackendManager, run_llm_prompt
from .ci_observation import CIConclusion, ObservationAvailability, WorkflowObservation
from .issue_context import IssueOracleResolution, VerifiedIssueOracle, get_linked_issues_context, resolve_issue_oracles
from .logger_config import get_logger
from .progress_footer import ProgressStage
from .prompt_loader import render_prompt
from .requirement_contract import build_normative_issue_manifest
from .reviewer_session_registry import RecoveredFileEvidence, ReviewerSession, ReviewerSessionRegistry, TestOracleGap
from .security_utils import redact_string
from .trace_logger import get_trace_logger
from .util.gh_cache import GitHubClient
from .utils import CommandExecutor

if TYPE_CHECKING:
    from .review_thread_validation import ClaimedReviewThread
    from .util.github_action import GitHubActionsStatusResult

logger = get_logger(__name__)

ADVERSARIAL_RESPONSE_PREVIEW_LIMIT = 2000
ADVERSARIAL_VALIDATION_COMMENT_LIMIT = 60000
ADVERSARIAL_VALIDATION_COMMENT_FIELD_LIMIT = 2000
ADVERSARIAL_VALIDATION_COVERAGE_ID_LIMIT = 20
ADVERSARIAL_VALIDATION_CACHE_VERSION = "v11"
ADVERSARIAL_ADJACENT_EXPLORATION_BUDGET = 8
ADVERSARIAL_EVIDENCE_RECOVERY_BUDGET = 300
CHANGE_PROVENANCE_CLARIFICATION_MARKER = "<!-- auto-coder-change-provenance-clarification:v1 -->"
TEST_ORACLE_GAP_STATUSES = {"OPEN", "RESOLVED", "INVALID"}
TEST_ORACLE_GAP_REREVIEW_EXCEPTIONS = {
    "CORRECTIVE_DIFF_NEW_BOUNDARY",
    "PROTECTION_WEAKENED",
    "REVALIDATION_EXPOSED_UNTESTED_BOUNDARY",
}


@dataclass
class AdversarialValidationFinding:
    """Demonstrated specification violation identified during validation."""

    requirement_id: str = ""
    requirement_ids: List[str] = field(default_factory=list)
    finding_identity: str = ""
    correction_identity: str = ""
    violated_requirement: str = ""
    reachability: str = ""
    required_behavior: str = ""
    actual_behavior: str = ""
    evidence: str = ""
    evidence_classification: str = "DEMONSTRATED"
    counterexample: str = ""
    test_gap: str = ""
    suggested_regression_scenario: str = ""
    anchor_path: str = ""
    anchor_line: Optional[int] = None
    anchor_side: str = "RIGHT"
    anchor_start_line: Optional[int] = None

    @property
    def all_requirement_ids(self) -> List[str]:
        """Return every requirement implicated by this concrete finding."""
        return list(dict.fromkeys([requirement_id for requirement_id in [self.requirement_id, *self.requirement_ids] if requirement_id]))


@dataclass
class IssueRequirement:
    """Stable requirement unit extracted deterministically from the Issue oracle."""

    requirement_id: str = ""
    text: str = ""


@dataclass
class DynamicCheckExecution:
    """A dynamic-check result bound to the repository revision it executed."""

    success: bool = False
    output: str = ""
    errors: str = ""
    executed_sha: Optional[str] = None
    verification_error: Optional[str] = None
    target_selection_error: Optional[str] = None


CANONICAL_PR_TESTS_WORKFLOW = ".github/workflows/pr-tests.yml"


def validate_dynamic_check_target(check_target: str, repository_root: Path) -> Optional[str]:
    """Validate the intentionally narrow reviewer-to-test-runner protocol."""
    if check_target == "all":
        return None
    if not check_target or check_target != check_target.strip():
        return "target must be 'all' or one repository-relative tests path"
    if any(character.isspace() for character in check_target) or any(character in check_target for character in ";|&`$<>\\"):
        return "target contains prose, whitespace, or shell/control syntax"
    path_text, *selectors = check_target.split("::")
    if selectors and (not path_text.endswith(".py") or any(not selector for selector in selectors)):
        return "pytest node selectors require a .py file and non-empty components"
    candidate = Path(path_text)
    if candidate.is_absolute() or not candidate.parts or candidate.parts[0] != "tests" or ".." in candidate.parts:
        return "target must resolve inside the repository tests/ tree"
    tests_root = (repository_root / "tests").resolve()
    resolved = (repository_root / candidate).resolve()
    if not resolved.is_relative_to(tests_root):
        return "target resolves outside the repository tests/ tree"
    if not resolved.exists():
        return "requested test file or directory does not exist"
    if resolved.is_file() and resolved.suffix != ".py":
        return "requested test file must have a .py suffix"
    if not resolved.is_file() and not resolved.is_dir():
        return "requested target is not a regular file or directory"
    return None


def canonical_pr_tests_succeeded(status: Optional["GitHubActionsStatusResult"]) -> bool:
    """Require pass eligibility and verified canonical workflow provenance."""
    if status is None or not status.success or status.in_progress or status.error or status.observation is None:
        return False
    snapshot = status.observation
    if snapshot.availability is not ObservationAvailability.KNOWN:
        return False
    newest: Dict[tuple[str, str], int] = {}
    for fact in snapshot.facts:
        if isinstance(fact, WorkflowObservation) and fact.execution.attempt is not None:
            key = (fact.execution.workflow_id, fact.execution.run_id)
            newest[key] = max(newest.get(key, 0), fact.execution.attempt)
    return any(isinstance(fact, WorkflowObservation) and fact.execution.attempt == newest.get((fact.execution.workflow_id, fact.execution.run_id)) and fact.workflow_path == CANONICAL_PR_TESTS_WORKFLOW and fact.conclusion is CIConclusion.SUCCESS for fact in snapshot.facts)


def focused_ci_target_succeeded(status: Optional["GitHubActionsStatusResult"], target: str) -> bool:
    """Return true only for an exact target explicitly reported by passing CI."""
    if not canonical_pr_tests_succeeded(status) or status is None or status.observation is None:
        return False
    newest: Dict[tuple[str, str], int] = {}
    for fact in status.observation.facts:
        if isinstance(fact, WorkflowObservation) and fact.execution.attempt is not None:
            key = (fact.execution.workflow_id, fact.execution.run_id)
            newest[key] = max(newest.get(key, 0), fact.execution.attempt)
    return any(
        isinstance(fact, WorkflowObservation) and fact.conclusion is CIConclusion.SUCCESS and fact.workflow_path == CANONICAL_PR_TESTS_WORKFLOW and fact.execution.attempt == newest.get((fact.execution.workflow_id, fact.execution.run_id)) and target in fact.successful_test_targets
        for fact in status.observation.facts
    )


def _ci_decision_evidence_equivalent(presented: Optional["GitHubActionsStatusResult"], current: Optional["GitHubActionsStatusResult"]) -> bool:
    """Compare execution facts while allowing fresh authority/read identities."""
    if presented is None or current is None or presented.observation is None or current.observation is None:
        return False
    before = presented.observation
    after = current.observation
    return (
        before.availability is ObservationAvailability.KNOWN
        and after.availability is ObservationAvailability.KNOWN
        and before.subject == after.subject
        and sorted(repr(fact) for fact in before.facts) == sorted(repr(fact) for fact in after.facts)
        and presented.success == current.success
        and presented.in_progress == current.in_progress
        and presented.error == current.error
    )


def format_ci_execution_evidence(status: Optional["GitHubActionsStatusResult"]) -> str:
    """Serialize the authoritative observation without flattening provider facts."""
    if status is None or status.observation is None:
        return json.dumps({"availability": "unavailable", "diagnostic": "No authoritative CI observation was supplied"}, sort_keys=True)
    snapshot = status.observation
    facts = []
    newest_attempt: Dict[tuple[str, str], int] = {}
    for fact in snapshot.facts:
        if isinstance(fact, WorkflowObservation) and fact.execution.attempt is not None:
            key = (fact.execution.workflow_id, fact.execution.run_id)
            newest_attempt[key] = max(newest_attempt.get(key, 0), fact.execution.attempt)
    for fact in snapshot.facts:
        if isinstance(fact, WorkflowObservation):
            actionable = fact.execution.attempt == newest_attempt.get((fact.execution.workflow_id, fact.execution.run_id))
            facts.append(
                {
                    "kind": "workflow",
                    "workflow_id": fact.execution.workflow_id,
                    "run_id": fact.execution.run_id,
                    "run_attempt": fact.execution.attempt,
                    "workflow_path": fact.workflow_path,
                    "conclusion": fact.conclusion.value,
                    "actionable": actionable,
                    "successful_test_targets": list(fact.successful_test_targets),
                }
            )
        else:
            facts.append(
                {
                    "kind": "check",
                    "app_id": fact.execution.app_id,
                    "check_id": fact.execution.check_id,
                    "workflow_id": fact.execution.workflow_id,
                    "run_id": fact.execution.run_id,
                    "run_attempt": fact.execution.attempt,
                    "conclusion": fact.conclusion.value,
                    "actionable": True,
                }
            )
    return json.dumps(
        {
            "subject": {
                "api_origin": snapshot.subject.api_origin,
                "repository": snapshot.subject.repository,
                "pr_number": snapshot.subject.pr_number,
                "head_sha": snapshot.subject.head_sha,
            },
            "observation": {
                "cycle_id": snapshot.cycle_id,
                "invalidation_epoch": snapshot.invalidation_epoch,
                "availability": snapshot.availability.value,
                "request_source": snapshot.request.source,
                "request_representation": snapshot.request.representation,
            },
            "provider_facts": facts,
            "gate_pass_eligible": bool(status.success and not status.in_progress and not status.error),
        },
        sort_keys=True,
    )


def _pytest_target_selection_error(test_result: Any) -> Optional[str]:
    """Recognize pure pytest selection failures without hiding collection errors."""
    output = f"{test_result.stdout}\n{test_result.stderr}".lower()
    collection_failure = any(marker in output for marker in ("error collecting", "collection error", "importerror while importing", "modulenotfounderror"))
    selection_failure = test_result.returncode in {4, 5} and any(
        marker in output
        for marker in (
            "not found:",
            "found no collectors",
            "no tests ran",
            "collected 0 items",
            "0 tests collected",
        )
    )
    if selection_failure and not collection_failure:
        return "pytest could not resolve the requested file/node or selected zero tests"
    return None


def run_exact_head_dynamic_check(
    config: AutomationConfig,
    check_target: str,
    expected_head_sha: str,
    execution_cwd: Optional[str] = None,
) -> DynamicCheckExecution:
    """Run a focused check locally only after proving the current exact HEAD.

    Adversarial validation enters a detached PR-head worktree before reaching
    this boundary.  Unlike ordinary local testing, this runner deliberately
    does not consult test-watcher or target-container routing: either could
    substitute evidence from a different checkout.  A second SHA assertion
    prevents a check that moved HEAD from being accepted as evidence.
    """
    target_error = validate_dynamic_check_target(check_target, Path(execution_cwd).resolve()) if execution_cwd else None
    if target_error:
        return DynamicCheckExecution(verification_error=f"dynamic-check target protocol error: {target_error}")
    executor = CommandExecutor()

    def current_head() -> Any:
        command = ["git", "rev-parse", "HEAD"]
        return executor.run_command(command, cwd=execution_cwd) if execution_cwd else executor.run_command(command)

    before = current_head()
    if not before.success or not before.stdout.strip():
        reason = (before.stderr or "git rev-parse HEAD returned no revision").strip()
        return DynamicCheckExecution(verification_error=reason)

    executed_sha = before.stdout.strip().lower()
    expected_sha = expected_head_sha.strip().lower()
    if executed_sha != expected_sha:
        return DynamicCheckExecution(
            executed_sha=executed_sha,
            verification_error=f"execution-target mismatch: expected {expected_sha}, found {executed_sha}",
        )

    command = ["bash", config.TEST_SCRIPT_PATH]
    if check_target != "all":
        command.append(check_target)
    # scripts/test.sh normally redirects from the Auto-Coder service container
    # to its configured target container.  This worktree is itself the verified
    # execution target, so explicitly suppress that redirect at the script's
    # supported boundary.  Otherwise the surrounding SHA assertions would
    # prove only the caller's checkout, not the repository that ran the tests.
    test_result = executor.run_command(
        command,
        timeout=executor.DEFAULT_TIMEOUTS["test"],
        env_overrides={"INSIDE_TARGET_EXECUTION": "true"},
        cwd=execution_cwd,
    )

    after = current_head()
    if not after.success or not after.stdout.strip():
        reason = (after.stderr or "git rev-parse HEAD returned no revision after the check").strip()
        return DynamicCheckExecution(
            success=test_result.success,
            output=test_result.stdout,
            errors=test_result.stderr,
            verification_error=reason,
        )
    final_sha = after.stdout.strip().lower()
    if final_sha != expected_sha:
        return DynamicCheckExecution(
            success=test_result.success,
            output=test_result.stdout,
            errors=test_result.stderr,
            executed_sha=final_sha,
            verification_error=f"execution-target mismatch after check: expected {expected_sha}, found {final_sha}",
        )
    return DynamicCheckExecution(
        success=test_result.success,
        output=test_result.stdout,
        errors=test_result.stderr,
        executed_sha=executed_sha,
        target_selection_error=_pytest_target_selection_error(test_result),
    )


@dataclass
class IssueRequirementManifest:
    """Authoritative requirements and diagnostics derived from direct Issues."""

    requirements: List[IssueRequirement] = field(default_factory=list)
    mode: str = "legacy-extraction"
    error: Optional[str] = None


@dataclass
class RequirementCoverageEntry:
    """Reviewer's structured disposition for one stable Issue requirement."""

    requirement_id: str = ""
    status: str = "UNVERIFIED"
    evidence: str = ""


@dataclass
class EvidenceRecoveryEntry:
    """Independent attempt to recover initially absent validator evidence."""

    path: str = ""
    source: str = ""
    status: str = "UNAVAILABLE"
    evidence: str = ""
    requirement_ids: List[str] = field(default_factory=list)


@dataclass
class DecisionCriticalEvidenceGap:
    """Smallest irreducible evidence gap preventing a correctness decision."""

    requirement_id: str = ""
    evidence_needed: str = ""
    recovery_attempts: List[str] = field(default_factory=list)


@dataclass
class ReviewThreadDisposition:
    """Validator's independent disposition for one claimed-addressed review thread.

    ``ADDRESSED`` means the original finding no longer holds on the validated
    head; ``STILL_VALID`` means the defect remains; ``INCONCLUSIVE`` means the
    available evidence could not establish either conclusion safely. This is
    orthogonal to the PR-level ``result`` (a thread may be ADDRESSED while the
    PR itself is NEEDS_FIX for an unrelated defect, and vice versa).
    """

    thread_id: str = ""
    status: str = ""  # "ADDRESSED", "STILL_VALID", "INCONCLUSIVE"
    rationale: str = ""
    evidence: str = ""


VALID_REVIEW_THREAD_DISPOSITION_STATUSES = {"ADDRESSED", "STILL_VALID", "INCONCLUSIVE"}


@dataclass
class SpecificationGap:
    """Material behavior for which the authoritative Issue defines no policy."""

    question: str = ""
    why_existing_issue_is_insufficient: str = ""
    observed_case: str = ""
    affected_scope: str = ""
    candidate_options: List[str] = field(default_factory=list)


@dataclass
class ChangeProvenanceItem:
    """One material changed file or related group whose PR provenance is unknown."""

    paths: List[str] = field(default_factory=list)
    change_group: str = ""
    why_unexplained: str = ""


@dataclass
class AdversarialValidationResult:
    """Outcome of adversarial PR validation against the specification oracle.

    Fail-closed design:
    - Only 'PASS' with 0 findings evaluates to is_pass=True.
    - Only findings carrying demonstrated reachability evidence normalize to 'NEEDS_FIX'.
    - Empty, malformed, 'BLOCKED', 'INCONCLUSIVE', or 'ERROR' evaluates to is_blocked=True.
    """

    result: str = "ERROR"  # "PASS", "NEEDS_FIX", "NEEDS_TESTS", "BLOCKED", "INCONCLUSIVE", "ERROR"
    summary: str = ""
    findings: List[AdversarialValidationFinding] = field(default_factory=list)
    raw_response: str = ""
    dynamic_check_requested: Optional[str] = None
    diagnostic_category: Optional[str] = None
    diagnostic_reason: Optional[str] = None
    requirement_coverage: List[RequirementCoverageEntry] = field(default_factory=list)
    evidence_recovery: List[EvidenceRecoveryEntry] = field(default_factory=list)
    decision_critical_evidence_gaps: List[DecisionCriticalEvidenceGap] = field(default_factory=list)
    specification_gaps: List[SpecificationGap] = field(default_factory=list)
    test_oracle_gaps: List[TestOracleGap] = field(default_factory=list)
    thread_dispositions: List[ReviewThreadDisposition] = field(default_factory=list)
    unexplained_changes: List[ChangeProvenanceItem] = field(default_factory=list)
    clarification_reply_fingerprint: str = ""
    publish_clarification_thread: bool = True
    provenance_thread_comment_ids: Dict[str, int] = field(default_factory=dict)
    attempt_id: str = ""
    attempt_sequence: int = 0
    reviewer_session_checkpoint: Optional[ReviewerSession] = field(default=None, repr=False)
    reviewer_session_registry: Optional[ReviewerSessionRegistry] = field(default=None, repr=False)

    @property
    def is_pass(self) -> bool:
        """Return whether all defined requirements passed (gaps are orthogonal)."""
        return self.result.strip().upper() == "PASS" and len(self.findings) == 0 and not self.open_test_oracle_gaps

    @property
    def open_test_oracle_gaps(self) -> List[TestOracleGap]:
        """Return material test-oracle gaps that still require regression protection."""
        return [gap for gap in self.test_oracle_gaps if gap.status == "OPEN"]

    @property
    def allows_auto_merge(self) -> bool:
        """Return whether this result satisfies every validation merge gate."""
        return self.is_pass and not self.specification_gaps

    @property
    def needs_fix(self) -> bool:
        """Return True if validation explicitly determined NEEDS_FIX with findings."""
        return self.result.strip().upper() == "NEEDS_FIX" and len(self.findings) > 0

    @property
    def needs_tests(self) -> bool:
        """Return True when focused regression protection is still required."""
        return self.result.strip().upper() in {"NEEDS_TESTS", "NEEDS_FIX"} and bool(self.open_test_oracle_gaps)

    @property
    def is_blocked(self) -> bool:
        """Return True if validation could not complete or produced non-pass status."""
        return not self.is_pass and not self.needs_fix and not self.needs_tests


@dataclass
class AdversarialValidationContext:
    """Context gathered for adversarial PR validation."""

    repo_name: str = ""
    pr_number: int = 0
    pr_title: str = ""
    pr_body: str = ""
    pr_diff: str = ""
    all_changed_files: List[str] = field(default_factory=list)
    changed_tests: List[str] = field(default_factory=list)
    issue_context: str = ""
    is_diff_truncated: bool = False
    unverified_files: List[str] = field(default_factory=list)
    issue_requirements: List[IssueRequirement] = field(default_factory=list)
    requirement_manifest_mode: str = "legacy-extraction"
    requirement_manifest_error: Optional[str] = None
    evidence_retrieval_error: Optional[str] = None
    requires_human_review: bool = False
    validation_snapshot: str = ""
    controller_file_evidence: List[FileDiffEvidence] = field(default_factory=list)

    @property
    def has_complete_file_coverage(self) -> bool:
        """Return whether every changed file has complete patch evidence."""
        return not self.unverified_files


@dataclass
class FileDiffEvidence:
    """Bounded patch evidence and coverage state for one changed file."""

    path: str = ""
    patch: str = ""
    original_size: int = 0
    is_complete: bool = False


def _snapshot_digest(repo_name: str, pr_data: Dict[str, Any], changed_files: Sequence[Dict[str, Any]], requirements: Sequence[IssueRequirement]) -> str:
    """Bind adjudication inputs to one deterministic PR/contract snapshot."""
    identity = {
        "repository": repo_name,
        "pr_number": pr_data.get("number"),
        "head_sha": (pr_data.get("head") or {}).get("sha") or pr_data.get("head_sha"),
        "base_sha": (pr_data.get("base") or {}).get("sha") or pr_data.get("base_sha"),
        "files": changed_files,
        "requirements": [(item.requirement_id, item.text) for item in requirements],
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()


def _github_file_record_has_complete_patch(record: Dict[str, Any]) -> bool:
    """Reject absent or truncated REST patches using authoritative line totals."""
    patch = record.get("patch")
    additions = record.get("additions")
    deletions = record.get("deletions")
    if not isinstance(patch, str) or not patch.strip() or not isinstance(additions, int) or not isinstance(deletions, int):
        return False
    patch_additions = sum(1 for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
    patch_deletions = sum(1 for line in patch.splitlines() if line.startswith("-") and not line.startswith("---"))
    return patch_additions == additions and patch_deletions == deletions


def adversarial_validation_comment_marker(head_sha: str) -> str:
    """Return the versioned marker used to deduplicate validation comments."""
    return f"<!-- auto-coder-adversarial-validation:{ADVERSARIAL_VALIDATION_CACHE_VERSION}:{head_sha} -->"


def adversarial_validation_codex_feedback_marker(head_sha: str) -> str:
    """Return the durable marker for a validation result sent to Codex Cloud."""
    return f"<!-- auto-coder-adversarial-validation-codex-feedback:{ADVERSARIAL_VALIDATION_CACHE_VERSION}:{head_sha} -->"


def count_adversarial_validation_comments(comments: Any) -> int:
    """Count the number of adversarial validation verdict comments on a PR."""
    if not comments:
        return 0
    items: Iterable[Any]
    if isinstance(comments, (list, tuple)):
        items = comments
    else:
        try:
            items = list(comments)
        except Exception:
            return 0

    count = 0
    for comment in items:
        body = comment.get("body", "") if isinstance(comment, dict) else getattr(comment, "body", "")
        if not isinstance(body, str):
            continue
        if re.search(r"<!--\s*auto-coder-adversarial-validation:(?!codex-feedback)[^>]+-->", body):
            count += 1
    return count


def _bounded_comment_field(value: str) -> str:
    """Return redacted, bounded prose without embedding raw Codex JSONL."""
    text = redact_string(value).strip()
    event_match = re.search(r'\{"type":"(?:thread|turn|item)\.', text)
    if event_match:
        prefix = text[: event_match.start()].rstrip(" :\n")
        omission = "_Codex CLI event details omitted; see the structured interaction log._"
        return f"{prefix}.\n\n{omission}" if prefix else omission
    if len(text) <= ADVERSARIAL_VALIDATION_COMMENT_FIELD_LIMIT:
        return text
    omitted = len(text) - ADVERSARIAL_VALIDATION_COMMENT_FIELD_LIMIT
    return f"{text[:ADVERSARIAL_VALIDATION_COMMENT_FIELD_LIMIT]}... [{omitted} characters omitted]"


def format_adversarial_validation_comment(result: AdversarialValidationResult, head_sha: str) -> str:
    """Render a bounded, human-readable PR comment for a validation result.

    The raw CLI response is intentionally excluded because it can contain a very
    large event stream.  The parsed summary and every structured finding retain
    the actionable reviewer output while keeping the GitHub comment usable.
    """
    status = result.result.strip().upper() or "ERROR"
    status_icon = "✅" if result.is_pass else "❌" if result.needs_fix else "⚠️"
    lines = [
        adversarial_validation_comment_marker(head_sha),
        *([f"<!-- auto-coder-adversarial-validation-attempt:v1:{result.attempt_sequence}:{result.attempt_id} -->"] if result.attempt_id else []),
        f"## {status_icon} Auto-Coder adversarial validation: {status}",
        "",
        f"Validated commit: `{head_sha}`",
        "",
        _bounded_comment_field(result.summary) or "No validation summary was provided.",
    ]

    if result.dynamic_check_requested:
        lines.extend(["", f"Dynamic check requested: `{_bounded_comment_field(result.dynamic_check_requested)}`"])

    if result.requirement_coverage:
        coverage_by_status: Dict[str, List[str]] = {}
        for entry in result.requirement_coverage:
            status = entry.status.strip().upper() or "UNVERIFIED"
            coverage_by_status.setdefault(status, []).append(entry.requirement_id)
        counts = ", ".join(f"{status}: {len(requirement_ids)}" for status, requirement_ids in sorted(coverage_by_status.items()))
        lines.extend(["", f"Issue requirement coverage ({len(result.requirement_coverage)} total): {counts}."])
        for status, requirement_ids in sorted(coverage_by_status.items()):
            if status in {"VERIFIED", "IRRELEVANT"}:
                continue
            displayed_ids = requirement_ids[:ADVERSARIAL_VALIDATION_COVERAGE_ID_LIMIT]
            omitted = len(requirement_ids) - len(displayed_ids)
            suffix = f" (+{omitted} more)" if omitted else ""
            lines.append(f"- **{status}**: {', '.join(f'`{requirement_id}`' for requirement_id in displayed_ids)}{suffix}")

    if result.findings:
        lines.extend(["", f"### Findings ({len(result.findings)})"])
        for index, finding in enumerate(result.findings, start=1):
            requirement_label = ", ".join(f"`{requirement_id}`" for requirement_id in finding.all_requirement_ids)
            requirement_label = f"{requirement_label}: " if requirement_label else ""
            lines.extend(["", f"#### {index}. Violated requirement", f"{requirement_label}{_bounded_comment_field(finding.violated_requirement) or 'Not specified.'}"])
            if finding.counterexample.strip():
                lines.extend(["", "**Counterexample**", "", _bounded_comment_field(finding.counterexample)])
            if finding.reachability.strip():
                lines.extend(["", "**Reachable path**", "", _bounded_comment_field(finding.reachability)])
            if finding.evidence.strip():
                lines.extend(["", "**Demonstrating evidence**", "", _bounded_comment_field(finding.evidence)])
            if finding.test_gap.strip():
                lines.extend(["", "**Test gap**", "", _bounded_comment_field(finding.test_gap)])
            if finding.suggested_regression_scenario.strip():
                lines.extend(["", "**Suggested regression scenario**", "", _bounded_comment_field(finding.suggested_regression_scenario)])

    if result.open_test_oracle_gaps:
        lines.extend(
            [
                "",
                f"### Material test-oracle gaps ({len(result.open_test_oracle_gaps)})",
                "",
                "These are missing regression protections, not demonstrated production-code violations. " "Automatic merge remains blocked pending the focused tests below.",
            ]
        )
        for index, oracle_gap in enumerate(result.open_test_oracle_gaps, start=1):
            lines.extend(
                [
                    "",
                    f"#### Gap {index}: `{oracle_gap.gap_id}` (`{oracle_gap.requirement_id}`)",
                    f"- **Authoritative boundary:** {_bounded_comment_field(oracle_gap.authoritative_boundary)}",
                    f"- **Invariant:** {_bounded_comment_field(oracle_gap.invariant)}",
                    f"- **Incorrect implementation that can pass:** {_bounded_comment_field(oracle_gap.plausible_incorrect_implementation)}",
                    f"- **Why tests remain green:** {_bounded_comment_field(oracle_gap.why_tests_still_pass)}",
                    f"- **Material consequence:** {_bounded_comment_field(oracle_gap.material_consequence)}",
                    f"- **Focused regression scenario:** {_bounded_comment_field(oracle_gap.focused_regression_scenario)}",
                ]
            )

    if result.specification_gaps:
        lines.extend(
            [
                "",
                f"### Specification gaps ({len(result.specification_gaps)})",
                "",
                "These are not proven implementation defects. The linked Issue does not define the required policy, "
                "and Auto-Coder did not choose one. Review of all defined requirements continued. Automatic merge is "
                "disabled while any gap remains; a human may still merge manually and handle specification follow-up separately.",
            ]
        )
        for index, gap in enumerate(result.specification_gaps, start=1):
            lines.extend(
                [
                    "",
                    f"#### Gap {index}: {_bounded_comment_field(gap.question)}",
                    f"- **Why the Issue is insufficient:** {_bounded_comment_field(gap.why_existing_issue_is_insufficient)}",
                    f"- **Observed case:** {_bounded_comment_field(gap.observed_case)}",
                    f"- **Affected scope:** {_bounded_comment_field(gap.affected_scope)}",
                ]
            )
            if gap.candidate_options:
                lines.append("- **Neutral candidate options (not requirements):**")
                lines.extend(f"  - {_bounded_comment_field(option)}" for option in gap.candidate_options)

    if result.diagnostic_category or result.diagnostic_reason:
        lines.extend(["", "### Diagnostic"])
        if result.diagnostic_category:
            lines.append(f"Category: `{result.diagnostic_category}`")
        if result.diagnostic_reason:
            lines.append(f"Reason: {_bounded_comment_field(result.diagnostic_reason)}")

    comment = "\n".join(lines)
    if len(comment) > ADVERSARIAL_VALIDATION_COMMENT_LIMIT:
        suffix = "\n\n_Comment truncated by Auto-Coder; the complete response remains in the structured LLM interaction log._"
        comment = comment[: ADVERSARIAL_VALIDATION_COMMENT_LIMIT - len(suffix)].rstrip() + suffix
    return comment


def format_adversarial_review_summary(
    result: AdversarialValidationResult,
    head_sha: str,
    attached_test_oracle_gap_count: Optional[int] = None,
) -> str:
    """Render review-level metadata without duplicating actionable findings."""
    summary_result = AdversarialValidationResult(
        result=result.result,
        summary=result.summary,
        dynamic_check_requested=result.dynamic_check_requested,
        diagnostic_category=result.diagnostic_category,
        diagnostic_reason=result.diagnostic_reason,
        requirement_coverage=result.requirement_coverage,
        specification_gaps=result.specification_gaps,
        attempt_id=result.attempt_id,
        attempt_sequence=result.attempt_sequence,
    )
    body = format_adversarial_validation_comment(summary_result, head_sha)
    if result.clarification_reply_fingerprint:
        body += f"\n{result.clarification_reply_fingerprint}"
    if result.findings:
        body += f"\n\n{len(result.findings)} actionable finding thread(s) are attached to this review."
    attached_gap_count = len(result.open_test_oracle_gaps) if attached_test_oracle_gap_count is None else attached_test_oracle_gap_count
    if attached_gap_count:
        body += f"\n\n{attached_gap_count} focused regression-test request thread(s) are attached to this review."
    elif result.open_test_oracle_gaps:
        body += f"\n\n{len(result.open_test_oracle_gaps)} open material test-oracle gap(s) remain represented by existing review threads."
    if result.unexplained_changes and result.publish_clarification_thread:
        body += "\n\nOne change-provenance clarification thread is attached to this review."
    unresolved_dispositions = [disposition for disposition in result.thread_dispositions if disposition.status != "ADDRESSED"]
    if unresolved_dispositions:
        body += "\n\n### Unresolved review-thread dispositions"
        for disposition in unresolved_dispositions:
            body += "\n\n" + "\n".join(
                [
                    f"#### `{_bounded_comment_field(disposition.thread_id)}`: {disposition.status}",
                    "",
                    "**Current rationale**",
                    "",
                    _bounded_comment_field(disposition.rationale),
                    "",
                    "**Current evidence**",
                    "",
                    _bounded_comment_field(disposition.evidence),
                ]
            )
    return body


def format_change_provenance_clarification(items: List[ChangeProvenanceItem]) -> str:
    """Render one aggregated, non-defect clarification thread."""
    lines = [
        CHANGE_PROVENANCE_CLARIFICATION_MARKER,
        "### Auto-Coder change-provenance clarification",
        "",
        "The Issue requirements were verified, but the reviewer could not independently establish why every material change below belongs to this PR. This is a clarification blocker, not an instruction to change code or create a commit.",
        "",
        "Please explain each item and classify it as:",
        "",
        "- intentional and directly required by the PR, including the causal reason;",
        "- generated or mechanically derived from another intentional change, including that causal change; or",
        "- unrelated or accidental.",
    ]
    for index, item in enumerate(items, start=1):
        paths = ", ".join(f"`{path}`" for path in item.paths)
        lines.extend(
            [
                "",
                f"{index}. **{_bounded_comment_field(item.change_group)}** — {paths}",
                f"   - Why clarification is needed: {_bounded_comment_field(item.why_unexplained)}",
            ]
        )
    lines.extend(
        [
            "",
            "Reply in this thread with the requested provenance. End a material provenance reply with the following marker on its own line so Auto-Coder can independently revalidate this same commit:",
            "",
            "`<!-- auto-coder-review-addressed:v1 -->`",
            "",
            "Your explanation is an unverified claim and navigation aid; the adversarial reviewer will compare it independently with the diff, repository state, and relevant tests before clearing this blocker.",
        ]
    )
    return "\n".join(lines)


def format_change_provenance_disposition(disposition: ReviewThreadDisposition, validated_head_sha: str) -> str:
    """Render an independent unresolved result for the clarification thread."""
    correction = "The identified accidental or contradicted material change must be removed or corrected before merge." if disposition.status == "STILL_VALID" else "The supplied explanation was insufficient to clear this clarification blocker."
    return "\n".join(
        [
            "<!-- auto-coder-change-provenance-disposition:v1 -->",
            "### Auto-Coder independent provenance verification",
            "",
            f"Validated commit: `{validated_head_sha}`",
            "",
            f"Status: **{disposition.status}**",
            "",
            correction,
            "",
            "**Rationale**",
            "",
            disposition.rationale,
            "",
            "**Evidence**",
            "",
            disposition.evidence,
        ]
    )


def format_adversarial_finding_comment(finding: AdversarialValidationFinding) -> str:
    """Render all actionable data for one independently resolvable thread."""
    requirement_label = ", ".join(f"`{requirement_id}`" for requirement_id in finding.all_requirement_ids)
    requirement_label = f"{requirement_label}: " if requirement_label else ""
    lines = [
        "### Auto-Coder adversarial finding",
        "",
        "**Violated requirement**",
        "",
        f"{requirement_label}{_bounded_comment_field(finding.violated_requirement) or 'Not specified.'}",
    ]
    fields = (
        ("Required behavior", finding.required_behavior),
        ("Actual behavior", finding.actual_behavior),
        ("Counterexample", finding.counterexample),
        ("Reachable path", finding.reachability),
        ("Demonstrating evidence", finding.evidence),
        ("Test gap", finding.test_gap),
        ("Suggested regression scenario", finding.suggested_regression_scenario),
    )
    for heading, value in fields:
        if value.strip():
            lines.extend(["", f"**{heading}**", "", _bounded_comment_field(value)])
    return "\n".join(lines)


def format_test_oracle_gap_comment(gap: TestOracleGap) -> str:
    """Render one independently resolvable focused regression-test request."""
    return "\n".join(
        [
            "### Auto-Coder material test-oracle gap",
            "",
            f"Gap identity: `{gap.gap_id}`",
            "",
            "This finding does **not** claim that current production behavior violates the Issue. " "It identifies missing regression protection for an explicit material requirement.",
            "",
            "**Issue requirement**",
            "",
            f"`{gap.requirement_id}`: {_bounded_comment_field(gap.requirement_text)}",
            "",
            "**Authoritative boundary**",
            "",
            _bounded_comment_field(gap.authoritative_boundary),
            "",
            "**Protected invariant**",
            "",
            _bounded_comment_field(gap.invariant),
            "",
            "**Minimal plausible incorrect implementation**",
            "",
            _bounded_comment_field(gap.plausible_incorrect_implementation),
            "",
            "**Why the available tests would still pass**",
            "",
            _bounded_comment_field(gap.why_tests_still_pass),
            "",
            "**Material consequence**",
            "",
            _bounded_comment_field(gap.material_consequence),
            "",
            "**Focused regression scenario requested**",
            "",
            _bounded_comment_field(gap.focused_regression_scenario),
            "",
            "Add only the focused regression protection described above; do not change production code unless a separate demonstrated violation requires it.",
        ]
    )


def is_test_file(file_path: str) -> bool:
    """Determine whether a file path corresponds to a test file."""
    path_lower = file_path.lower()
    return path_lower.startswith("tests/") or path_lower.startswith("test/") or "/tests/" in path_lower or "/test/" in path_lower or path_lower.endswith("_test.py") or path_lower.endswith("test.py") or ".spec." in path_lower or ".test." in path_lower


def _decode_git_path_token(token: str) -> str:
    """Decode a pathname token emitted with Git's ``core.quotePath`` format."""
    value = token.strip()
    if not (value.startswith('"') and value.endswith('"')):
        return value

    try:
        decoded = ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return value
    if not isinstance(decoded, str):
        return value

    # Git writes non-ASCII UTF-8 bytes as C-style octal escapes. Python's
    # string-literal decoder maps each byte to the same Latin-1 code point, so
    # convert those code points back to bytes before decoding the pathname.
    try:
        return decoded.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return decoded


def _extract_diff_header_path(line: str) -> Optional[str]:
    """Extract the destination path from quoted or unquoted ``diff --git`` headers."""
    prefix = "diff --git "
    if not line.startswith(prefix):
        return None
    payload = line[len(prefix) :]

    if payload.startswith('"'):
        quoted_pair = re.fullmatch(r'("(?:\\.|[^"\\])*")\s+("(?:\\.|[^"\\])*")', payload)
        if not quoted_pair:
            return None
        destination = _decode_git_path_token(quoted_pair.group(2))
    else:
        unquoted_pair = re.fullmatch(r"a/(.*)\s+b/(.*)", payload)
        if not unquoted_pair:
            return None
        destination = f"b/{unquoted_pair.group(2)}"

    return destination[2:] if destination.startswith("b/") else destination


def _extract_marker_path(line: str) -> Optional[str]:
    """Extract a path from a unified diff ``+++`` destination marker."""
    if not line.startswith("+++ "):
        return None
    destination = _decode_git_path_token(line[4:])
    if destination == "/dev/null":
        return None
    return destination[2:] if destination.startswith("b/") else destination


def extract_all_changed_files(pr_diff: str) -> List[str]:
    """Extract all modified or added file paths from a unified diff string.

    Args:
        pr_diff: Unified diff string

    Returns:
        List of all changed file paths
    """
    if not pr_diff:
        return []

    files = [file_patch.path for file_patch in _split_diff_by_file(pr_diff)]
    if files:
        return files

    # Retain support for a bare unified patch without ``diff --git`` headers.
    for line in pr_diff.splitlines():
        path = _extract_marker_path(line)
        if path and path not in files:
            files.append(path)

    return files


def extract_changed_test_files(pr_diff: str) -> List[str]:
    """Extract test file paths modified or added in the PR diff.

    Args:
        pr_diff: Unified diff string

    Returns:
        List of changed test file paths
    """
    all_files = extract_all_changed_files(pr_diff)
    return [f for f in all_files if is_test_file(f)]


def extract_issue_requirements(issue_context: str) -> List[IssueRequirement]:
    """Create stable, machine-checkable requirement units from the Issue oracle.

    Every substantive oracle line is included, so a reviewer cannot silently omit
    a requirement-bearing prose line. Non-requirement background may be marked
    IRRELEVANT by the reviewer, but it still needs an explicit coverage entry.
    """
    requirements: List[IssueRequirement] = []
    seen: set[str] = set()
    ignored_labels = {"issue description:", "parent issue description:"}
    for raw_line in issue_context.splitlines():
        text = raw_line.strip()
        if not text or text.lower() in ignored_labels:
            continue
        normalized = re.sub(r"^#{1,6}\s+", "", text)
        normalized = re.sub(r"^(?:[-*+]\s+|\d+[.)]\s+|\[[ xX]\]\s+)", "", normalized).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        digest = hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()[:10]
        requirement_id = f"REQ-{len(requirements) + 1:03d}-{digest}"
        requirements.append(IssueRequirement(requirement_id=requirement_id, text=normalized))
    return requirements


def _explicit_requirements_section(issue: VerifiedIssueOracle) -> tuple[bool, List[tuple[str, str]], Optional[str]]:
    """Adapt the shared contract oracle to the adversarial manifest builder."""
    manifest = build_normative_issue_manifest(issue.number, issue.title, issue.body)
    entries = [(entry.requirement_id, entry.text) for entry in manifest.requirements]
    return manifest.explicit_contract_present, entries, manifest.error


def build_issue_requirement_manifest(resolution: IssueOracleResolution) -> IssueRequirementManifest:
    """Build a contract from direct Issue bodies, using legacy extraction only when needed."""
    parsed: List[tuple[VerifiedIssueOracle, bool, List[tuple[str, str]]]] = []
    modes: set[str] = set()
    for issue in resolution.issues:
        explicit, entries, error = _explicit_requirements_section(issue)
        if error:
            return IssueRequirementManifest(mode="explicit-contract", error=error)
        if explicit:
            modes.add("explicit-contract")
            parsed.append((issue, True, entries))
        else:
            modes.add("legacy-extraction")
            legacy = [(requirement.requirement_id, requirement.text) for requirement in extract_issue_requirements(issue.body)]
            parsed.append((issue, False, legacy))

    explicit_id_counts: Dict[str, int] = {}
    for _, explicit, entries in parsed:
        if explicit:
            for requirement_id, _ in entries:
                explicit_id_counts[requirement_id] = explicit_id_counts.get(requirement_id, 0) + 1

    requirements: List[IssueRequirement] = []
    for issue, explicit, entries in parsed:
        for requirement_id, requirement_text in entries:
            validator_id = f"#{issue.number}/{requirement_id}" if explicit and explicit_id_counts[requirement_id] > 1 else requirement_id
            requirements.append(IssueRequirement(requirement_id=validator_id, text=requirement_text))
    mode = "mixed" if len(modes) > 1 else next(iter(modes), "legacy-extraction")
    return IssueRequirementManifest(requirements=requirements, mode=mode)


def _split_diff_by_file(pr_diff: str) -> List[FileDiffEvidence]:
    """Split a unified Git diff into ordered per-file patches."""
    matches = list(re.finditer(r"^diff --git .*$", pr_diff, re.MULTILINE))
    evidence: List[FileDiffEvidence] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(pr_diff)
        patch = pr_diff[match.start() : end].rstrip()
        path = next((_extract_marker_path(line) for line in patch.splitlines() if _extract_marker_path(line)), None)
        path = path or _extract_diff_header_path(match.group(0)) or f"<unparsed diff header at character {match.start()}>"
        evidence.append(FileDiffEvidence(path=path, patch=patch, original_size=len(patch)))
    return evidence


def _render_complete_file_evidence(file_patch: FileDiffEvidence) -> str:
    """Render one completely covered file patch."""
    return f"### Changed file: {file_patch.path}\nCoverage: COMPLETE ({file_patch.original_size}/{file_patch.original_size} patch characters supplied)\n{file_patch.patch}"


def _format_path_manifest(paths: List[str], empty_message: str) -> str:
    """Render a complete path manifest with no local character bound.

    REQ-001/REQ-002: every path Auto-Coder already retrieved must reach the
    reviewer; a local character budget must never omit, truncate, or sample
    the changed-file manifest.
    """
    if not paths:
        return empty_message
    return "\n".join(f"- {path}" for path in paths)


def build_file_aware_diff(pr_diff: str) -> tuple[str, List[str]]:
    """Render complete, per-file diff evidence for every retrieved changed file.

    REQ-001/REQ-002: a local character budget must never convert already
    retrieved patch evidence into unverified evidence, so every file
    ``_split_diff_by_file`` can identify is rendered with its complete,
    untruncated patch. The unverified-files return value is always empty
    here; only a genuine upstream retrieval gap (see
    ``build_adversarial_validation_context``) may report a path as
    unverified.
    """
    file_patches = _split_diff_by_file(pr_diff)
    if not file_patches:
        return pr_diff, []

    complete_blocks = [_render_complete_file_evidence(file_patch) for file_patch in file_patches]
    return "\n\n".join(complete_blocks), []


def build_adversarial_validation_context(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    github_client: Optional[Any] = None,
) -> AdversarialValidationContext:
    """Compile issue specification, PR diff, and changed tests for validation.

    Recovers oracle/specification from PR body, title, branch name, and session.
    Every changed-file patch that GitHub's diff endpoint returns is supplied in
    full; no Auto-Coder-local character budget hides or samples a file.

    Args:
        repo_name: Repository name in 'owner/repo' format
        pr_data: Pull request metadata dictionary
        config: AutomationConfig instance
        github_client: Optional GitHubClient instance

    Returns:
        AdversarialValidationContext populated with review data
    """
    pr_number = pr_data.get("number", 0)
    pr_title = pr_data.get("title", "Unknown")
    pr_body = pr_data.get("body") or ""

    client = github_client
    if client is None:
        try:
            client = GitHubClient.get_instance()
        except Exception:
            client = None

    # Retrieve complete diff evidence and the authoritative changed-file count through
    # independent endpoints so GitHub's own raw-diff retrieval gap cannot authorize a
    # large PR. No Auto-Coder-local character budget is applied to either (REQ-001,
    # REQ-002): a path only becomes unverified when the authoritative changed-file
    # count exceeds what the raw diff actually returned (a genuine upstream gap).
    pr_diff = ""
    is_diff_truncated = False
    all_changed_files: List[str] = []
    unverified_files: List[str] = []
    evidence_retrieval_error: Optional[str] = None
    requires_human_review = False
    changed_file_records: List[Dict[str, Any]] = []
    controller_file_evidence: List[FileDiffEvidence] = []

    if client:
        raw_diff = ""
        diff_changed_files: List[str] = []
        try:
            raw_diff = client.get_pr_diff(repo_name, pr_number)
            if raw_diff:
                diff_changed_files = extract_all_changed_files(raw_diff)
                pr_diff, unverified_files = build_file_aware_diff(raw_diff)
        except Exception as e:
            logger.warning(f"Could not retrieve diff for PR #{pr_number}: {e}")

        all_changed_files = diff_changed_files
        try:
            changed_file_count = client.get_pr_changed_file_count(repo_name, pr_number)
            if changed_file_count > 300:
                requires_human_review = True
            elif changed_file_count > len(diff_changed_files):
                records = client.get_pr_changed_files(repo_name, pr_number)
                if not isinstance(records, list) or len(records) != changed_file_count:
                    raise ValueError("authoritative changed-file listing was incomplete")
                changed_file_records = [dict(record) for record in records]
                listed_paths = [str(record.get("filename", "")).strip() for record in changed_file_records]
                if any(not path for path in listed_paths):
                    raise ValueError("authoritative changed-file listing contained an unnamed path")
                all_changed_files = listed_paths
                raw_paths = set(diff_changed_files)
                unverified_files = [path for path in listed_paths if path not in raw_paths]
                by_path = {str(record["filename"]): record for record in changed_file_records}
                for path in unverified_files:
                    record = by_path[path]
                    complete = _github_file_record_has_complete_patch(record)
                    evidence = json.dumps(record, sort_keys=True, default=str)
                    controller_file_evidence.append(FileDiffEvidence(path=path, patch=evidence, original_size=len(evidence), is_complete=complete))
            elif changed_file_count == len(diff_changed_files):
                raw_digest = hashlib.sha256(pr_diff.encode()).hexdigest()
                changed_file_records = [{"filename": path, "raw_diff_sha256": raw_digest} for path in diff_changed_files]
        except Exception as e:
            evidence_retrieval_error = f"Authoritative changed-file count retrieval failed: {e}"
            logger.warning(f"Could not retrieve authoritative changed-file count for PR #{pr_number}: {e}")

        is_diff_truncated = bool(unverified_files)

    # Extract linked issues context (from body, title, branch name, session)
    resolution = resolve_issue_oracles(client, repo_name, pr_data=pr_data, pr_body=pr_body)
    issue_context = get_linked_issues_context(client, repo_name, pr_body=pr_body, pr_data=pr_data, resolution=resolution)
    manifest = build_issue_requirement_manifest(resolution)

    # Extract changed test files from diff or all_changed_files
    changed_tests = [f for f in all_changed_files if is_test_file(f)] if all_changed_files else extract_changed_test_files(pr_diff)

    return AdversarialValidationContext(
        repo_name=repo_name,
        pr_number=pr_number,
        pr_title=pr_title,
        pr_body=pr_body,
        pr_diff=pr_diff,
        all_changed_files=all_changed_files,
        changed_tests=changed_tests,
        issue_context=issue_context,
        is_diff_truncated=is_diff_truncated,
        unverified_files=unverified_files,
        issue_requirements=manifest.requirements,
        requirement_manifest_mode=manifest.mode,
        requirement_manifest_error=manifest.error,
        evidence_retrieval_error=evidence_retrieval_error,
        requires_human_review=requires_human_review,
        validation_snapshot=_snapshot_digest(repo_name, pr_data, changed_file_records, manifest.requirements),
        controller_file_evidence=controller_file_evidence,
    )


def _extract_finding_from_dict(f: Dict[str, Any]) -> Optional[AdversarialValidationFinding]:
    """Helper to parse an AdversarialValidationFinding from a dictionary."""
    req = str(f.get("violated_requirement", "")).strip()
    ce = str(f.get("counterexample", "")).strip()
    gap = str(f.get("test_gap", "")).strip()
    scen = str(f.get("suggested_regression_scenario", "")).strip()

    if req or ce or gap or scen:
        return AdversarialValidationFinding(
            violated_requirement=req or "Specification requirement violated",
            counterexample=ce or "Specification violation identified",
            test_gap=gap or "Existing tests do not assert this condition",
            suggested_regression_scenario=scen or "Add regression test covering counterexample",
        )
    return None


def _extract_thread_dispositions(raw_value: Any) -> List[ReviewThreadDisposition]:
    """Parse review-thread dispositions leniently: a malformed entry is dropped.

    A thread disposition is orthogonal metadata (REQ-005): a malformed or
    incomplete entry must never corrupt the PR-level PASS/NEEDS_FIX verdict.
    Dropping it simply means that thread receives no valid disposition and
    therefore stays unresolved (REQ-004, REQ-008) rather than failing the
    entire validator response closed.
    """
    if not isinstance(raw_value, list):
        return []

    # Count raw thread_id occurrences BEFORE per-entry validation. A duplicate
    # thread_id must invalidate that thread even when one of the duplicates is
    # itself malformed (e.g. one valid ADDRESSED entry plus a second, invalid
    # entry for the same ID) — otherwise the malformed entry would be dropped
    # first and the single remaining valid entry would wrongly survive
    # (REQ-006, REQ-008).
    raw_thread_id_counts: Dict[str, int] = {}
    for item in raw_value:
        if not isinstance(item, dict):
            continue
        raw_thread_id = str(item.get("thread_id", "")).strip()
        if raw_thread_id:
            raw_thread_id_counts[raw_thread_id] = raw_thread_id_counts.get(raw_thread_id, 0) + 1
    duplicate_thread_ids = {thread_id for thread_id, count in raw_thread_id_counts.items() if count > 1}

    dispositions: List[ReviewThreadDisposition] = []
    for item in raw_value:
        if not isinstance(item, dict):
            logger.warning("Dropping malformed thread_dispositions entry: not an object")
            continue
        thread_id = str(item.get("thread_id", "")).strip()
        if thread_id in duplicate_thread_ids:
            # Fail closed: invalidate every entry for this thread rather than
            # keeping whichever valid one happened to parse (REQ-006, REQ-008).
            logger.warning(f"Dropping thread_dispositions entry for thread_id={thread_id!r}: {raw_thread_id_counts[thread_id]} contradictory/duplicate raw entries")
            continue
        status = str(item.get("status", "")).strip().upper()
        rationale = str(item.get("rationale", "")).strip()
        evidence = str(item.get("evidence", "")).strip()
        if not thread_id or status not in VALID_REVIEW_THREAD_DISPOSITION_STATUSES or not rationale or not evidence:
            logger.warning(f"Dropping malformed thread_dispositions entry for thread_id={thread_id!r}: incomplete or invalid fields")
            continue
        dispositions.append(ReviewThreadDisposition(thread_id=thread_id, status=status, rationale=rationale, evidence=evidence))
    return dispositions


def _optional_positive_int(value: Any) -> Optional[int]:
    """Normalize an optional positive line anchor from an LLM response."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("review anchor lines must be positive integers")
    if isinstance(value, str):
        if not re.fullmatch(r"[1-9][0-9]*", value):
            raise ValueError("review anchor lines must be positive integers")
        return int(value)
    if not isinstance(value, int) or value < 1:
        raise ValueError("review anchor lines must be positive integers")
    return value


def _bounded_response_preview(response: str) -> str:
    """Return a redacted, bounded response preview suitable for normal logs."""
    redacted = redact_string(response)
    if len(redacted) <= ADVERSARIAL_RESPONSE_PREVIEW_LIMIT:
        return redacted
    omitted = len(redacted) - ADVERSARIAL_RESPONSE_PREVIEW_LIMIT
    return f"{redacted[:ADVERSARIAL_RESPONSE_PREVIEW_LIMIT]}... [{omitted} characters omitted]"


def _extract_codex_jsonl_message(response: str) -> tuple[bool, Optional[str], Optional[str]]:
    """Extract the final assistant message from a Codex ``--json`` event stream.

    Returns ``(detected, message, error)``. Once a Codex JSONL envelope is
    detected, malformed lines and failed events are reported instead of being
    ignored so stderr contamination cannot turn an invalid response into PASS.
    """
    lines = [line for line in response.splitlines() if line.strip()]
    if not lines:
        return False, None, None

    try:
        first_event = json.loads(lines[0])
    except json.JSONDecodeError:
        return False, None, None

    if not isinstance(first_event, dict):
        return False, None, None
    first_type = first_event.get("type")
    if not isinstance(first_type, str) or not first_type.startswith(("thread.", "turn.", "item.")):
        return False, None, None

    messages: List[str] = []
    for line_number, line in enumerate(lines, start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            reason = f"non-JSON content in Codex event stream at line {line_number}: {exc.msg}"
            return True, None, reason
        if not isinstance(event, dict):
            return True, None, f"Codex event at line {line_number} is not an object"

        event_type = event.get("type")
        if event_type in ("turn.failed", "error"):
            return True, None, f"Codex emitted failure event {event_type} at line {line_number}"

        item = event.get("item")
        if event_type == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                messages.append(text)

    if not messages:
        return True, None, "Codex event stream contained no completed agent message"
    return True, messages[-1], None


def _extract_claude_jsonl_result(response: str) -> tuple[bool, Optional[str], Optional[str]]:
    """Extract Claude CLI's final result from a ``stream-json`` event stream.

    Claude streams one JSON object per line, beginning with ``system/init`` and
    ending with a ``result`` event.  Once that envelope is detected, every line
    and the terminal result are validated so truncated or failed streams cannot
    accidentally authorize a merge.
    """
    lines = [line for line in response.splitlines() if line.strip()]
    if not lines:
        return False, None, None

    try:
        first_event = json.loads(lines[0])
    except json.JSONDecodeError:
        return False, None, None

    if not isinstance(first_event, dict) or first_event.get("type") != "system" or first_event.get("subtype") != "init":
        return False, None, None

    terminal_results: List[tuple[int, Dict[str, Any]]] = []
    for line_number, line in enumerate(lines, start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            return True, None, f"non-JSON content in Claude event stream at line {line_number}: {exc.msg}"
        if not isinstance(event, dict):
            return True, None, f"Claude event at line {line_number} is not an object"
        if event.get("type") == "result":
            terminal_results.append((line_number, event))

    if not terminal_results:
        return True, None, "Claude event stream contained no terminal result"
    if len(terminal_results) != 1:
        return True, None, f"Claude event stream contained {len(terminal_results)} terminal results"

    line_number, terminal = terminal_results[0]
    if terminal.get("subtype") != "success" or terminal.get("is_error") is True:
        subtype = terminal.get("subtype", "unknown")
        return True, None, f"Claude emitted failed terminal result {subtype} at line {line_number}"
    result = terminal.get("result")
    if not isinstance(result, str) or not result.strip():
        return True, None, f"Claude terminal result at line {line_number} contained no response text"
    return True, result, None


def _parse_error(response: str, category: str, summary: str, reason: str) -> AdversarialValidationResult:
    """Create and diagnose a fail-closed adversarial parse result."""
    response_length = len(response)
    state = "empty" if not response.strip() else "non-empty"
    preview = _bounded_response_preview(response) if response else "<empty>"
    logger.warning(
        "Adversarial validator response failed parsing: category={}, reason={}, " "response_state={}, response_length={}, preview={!r}; failing closed to ERROR",
        category,
        reason,
        state,
        response_length,
        preview,
    )
    return AdversarialValidationResult(
        result="ERROR",
        summary=summary,
        raw_response=response,
        diagnostic_category=category,
        diagnostic_reason=reason,
    )


def _log_contextual_parse_diagnostics(
    result: AdversarialValidationResult,
    response: str,
    backend_manager: BackendManager,
    pr_number: int,
    attempt: str,
) -> None:
    """Log a bounded parse diagnostic correlated to a validation attempt."""
    if result.result != "ERROR" or not result.diagnostic_category:
        return

    backend: Optional[str] = None
    model: Optional[str] = None
    interaction_log: Optional[str] = None
    try:
        backend_value, model_value = backend_manager.get_last_backend_and_model()
        backend = backend_value if isinstance(backend_value, str) else None
        model = model_value if isinstance(model_value, str) else None
    except Exception:
        pass
    try:
        log_path = backend_manager.get_last_interaction_log_path()
        interaction_log = log_path if isinstance(log_path, str) else None
    except Exception:
        pass

    response_state = "empty" if not response.strip() else "non-empty"
    preview = _bounded_response_preview(response) if response else "<empty>"
    details = {
        "result": result.result,
        "attempt": attempt,
        "backend": backend or "unknown",
        "model": model or "unknown",
        "response_state": response_state,
        "response_length": len(response),
        "response_preview": preview,
        "diagnostic_category": result.diagnostic_category,
        "diagnostic_reason": result.diagnostic_reason or result.summary,
        "interaction_log": interaction_log or "unavailable",
    }
    logger.warning(
        "Adversarial validation parse failure for PR #{} (attempt={}, backend={}, model={}): " "category={}, reason={}, response_state={}, response_length={}, preview={!r}, " "full_response_log={}",
        pr_number,
        attempt,
        details["backend"],
        details["model"],
        details["diagnostic_category"],
        details["diagnostic_reason"],
        response_state,
        len(response),
        preview,
        details["interaction_log"],
    )
    get_trace_logger().log(
        "Adversarial Validation Parse Failure",
        f"Validator response parsing failed for PR #{pr_number} ({attempt})",
        item_type="pr",
        item_number=pr_number,
        details=details,
    )


def _stable_test_oracle_gap_id(requirement_id: str, authoritative_boundary: str, invariant: str) -> str:
    """Derive a stable identity from the authoritative missing-oracle scope."""
    normalized = "\0".join(re.sub(r"\s+", " ", value.strip()).casefold() for value in (requirement_id, authoritative_boundary, invariant))
    return f"TOG-{hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:12]}"


def _populate_test_oracle_gap_requirement_text(
    gaps: List[TestOracleGap],
    requirements: List[IssueRequirement],
) -> None:
    """Attach authoritative manifest text to gaps with known stable IDs."""
    requirement_text_by_id = {requirement.requirement_id: requirement.text for requirement in requirements}
    for gap in gaps:
        if gap.requirement_id in requirement_text_by_id:
            gap.requirement_text = requirement_text_by_id[gap.requirement_id]


def _extract_test_oracle_gaps(raw_value: object, raw_response: str) -> tuple[List[TestOracleGap], Optional[AdversarialValidationResult]]:
    """Parse and consolidate the distinct material test-oracle-gap schema."""
    if not isinstance(raw_value, list):
        return [], _parse_error(raw_response, "schema_error", "Malformed validator schema: test_oracle_gaps must be a list", "test_oracle_gaps must be a list")

    gaps: List[TestOracleGap] = []
    seen_ids: set[str] = set()
    required_fields = (
        "requirement_id",
        "authoritative_boundary",
        "invariant",
        "plausible_incorrect_implementation",
        "why_tests_still_pass",
        "material_consequence",
        "focused_regression_scenario",
        "anchor_path",
    )
    for item in raw_value:
        if not isinstance(item, dict):
            return [], _parse_error(raw_response, "schema_error", "Malformed test-oracle gap entry", "test-oracle gap entries must be objects")
        values = {name: str(item.get(name, "")).strip() for name in required_fields}
        missing = [name for name, value in values.items() if not value]
        if missing:
            return [], _parse_error(raw_response, "schema_error", "Malformed test-oracle gap entry", f"test-oracle gap is missing: {', '.join(missing)}")

        status = str(item.get("status", "OPEN")).strip().upper()
        phase = str(item.get("discovery_phase", "INITIAL")).strip().upper()
        exception_reason = str(item.get("rereview_exception_reason", "NONE")).strip().upper()
        exception_evidence = str(item.get("rereview_exception_evidence", "")).strip()
        resolution_evidence = str(item.get("resolution_evidence", "")).strip()
        if status not in TEST_ORACLE_GAP_STATUSES:
            return [], _parse_error(raw_response, "schema_error", "Malformed test-oracle gap status", f"unsupported test-oracle gap status: {status}")
        if phase not in {"INITIAL", "REREVIEW"}:
            return [], _parse_error(raw_response, "schema_error", "Malformed test-oracle gap discovery phase", f"unsupported discovery phase: {phase}")
        if status in {"RESOLVED", "INVALID"} and not resolution_evidence:
            return [], _parse_error(raw_response, "schema_error", "Resolved test-oracle gap lacks evidence", f"{status} requires resolution_evidence")

        derived_id = _stable_test_oracle_gap_id(values["requirement_id"], values["authoritative_boundary"], values["invariant"])
        supplied_id = str(item.get("gap_id", "")).strip()
        if status in {"RESOLVED", "INVALID"} and not supplied_id:
            return [], _parse_error(
                raw_response,
                "schema_error",
                "Resolved test-oracle gap lacks its stored identity",
                f"{status} requires an explicit gap_id",
            )
        if supplied_id and supplied_id != derived_id:
            return [], _parse_error(raw_response, "schema_error", "Unstable test-oracle gap identity", f"gap_id must be {derived_id} for the supplied scope")
        if derived_id in seen_ids:
            continue
        seen_ids.add(derived_id)
        gaps.append(
            TestOracleGap(
                gap_id=derived_id,
                **values,
                anchor_line=_optional_positive_int(item.get("anchor_line")),
                anchor_side=str(item.get("anchor_side", "RIGHT")).strip().upper(),
                anchor_start_line=_optional_positive_int(item.get("anchor_start_line")),
                discovery_phase=phase,
                rereview_exception_reason=exception_reason,
                rereview_exception_evidence=exception_evidence,
                status=status,
                resolution_evidence=resolution_evidence,
            )
        )
    return gaps, None


def parse_adversarial_validation_response(response: str) -> AdversarialValidationResult:
    """Parse the strong model's adversarial validation output.

    Fail-closed policy:
    - Entire response schema must be strictly valid and consistent.
    - PASS requires raw_result == "PASS" and findings to be strictly empty ([] or None).
    - Demonstrated, reachable findings override labels and normalize to NEEDS_FIX.
    - Explicitly unverified suspicions are discarded as findings and produce INCONCLUSIVE.
    - Malformed or empty finding elements fail closed to ERROR.
    - NEEDS_FIX requires at least one finding with a concrete counterexample; missing counterexamples are never synthesized.
    - Malformed or unparseable schemas fail closed to ERROR.

    Args:
        response: Raw LLM response string

    Returns:
        Parsed AdversarialValidationResult
    """
    if not response or not response.strip():
        return _parse_error(
            response or "",
            "empty_response",
            "Empty response received from validator; validation incomplete",
            "validator returned no non-whitespace content",
        )

    raw_response = response
    jsonl_detected, extracted_message, jsonl_error = _extract_codex_jsonl_message(raw_response)
    if jsonl_detected and jsonl_error:
        return _parse_error(
            raw_response,
            "cli_event_stream_error",
            f"Invalid Codex validator event stream: {jsonl_error}",
            jsonl_error,
        )
    if not jsonl_detected:
        jsonl_detected, extracted_message, jsonl_error = _extract_claude_jsonl_result(raw_response)
        if jsonl_detected and jsonl_error:
            return _parse_error(
                raw_response,
                "cli_event_stream_error",
                f"Invalid Claude validator event stream: {jsonl_error}",
                jsonl_error,
            )
    effective_response = extracted_message if jsonl_detected and extracted_message is not None else raw_response
    cleaned_response = effective_response.strip()

    # 1. Try parsing JSON block from markdown codeblock or direct JSON
    json_str = ""
    json_block_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned_response, re.DOTALL)
    if json_block_match:
        json_str = json_block_match.group(1)
    else:
        bare_match = re.search(r"(\{[\s\S]*\})", cleaned_response)
        if bare_match:
            json_str = bare_match.group(1)

    json_failure_reason: Optional[str] = None
    if json_str:
        try:
            parsed = json.loads(json_str)
            if isinstance(parsed, dict):
                raw_result = str(parsed.get("result", "")).strip().upper()
                summary = str(parsed.get("summary", "")).strip()
                dynamic_check = parsed.get("dynamic_check_requested")
                if dynamic_check:
                    dynamic_check = str(dynamic_check).strip()

                raw_specification_gaps = parsed.get("specification_gaps", [])
                if not isinstance(raw_specification_gaps, list):
                    return _parse_error(raw_response, "schema_error", "Malformed validator schema: specification_gaps must be a list", "specification_gaps must be a list")
                specification_gaps: List[SpecificationGap] = []
                for item in raw_specification_gaps:
                    if not isinstance(item, dict):
                        return _parse_error(raw_response, "schema_error", "Malformed specification gap entry", "specification gap entries must be objects")
                    values = {name: str(item.get(name, "")).strip() for name in ("question", "why_existing_issue_is_insufficient", "observed_case", "affected_scope")}
                    options = item.get("candidate_options", [])
                    if any(not value for value in values.values()) or not isinstance(options, list) or any(not isinstance(option, str) or not option.strip() for option in options):
                        return _parse_error(raw_response, "schema_error", "Malformed specification gap entry", "each specification gap requires all four descriptive fields and candidate_options must be a list of non-empty strings")
                    specification_gaps.append(SpecificationGap(**values, candidate_options=[option.strip() for option in options]))

                test_oracle_gaps, gap_parse_error = _extract_test_oracle_gaps(parsed.get("test_oracle_gaps", []), raw_response)
                if gap_parse_error is not None:
                    return gap_parse_error

                raw_unexplained_changes = parsed.get("unexplained_changes", [])
                if not isinstance(raw_unexplained_changes, list):
                    return _parse_error(raw_response, "schema_error", "Malformed validator schema: unexplained_changes must be a list", "unexplained_changes must be a list")
                unexplained_changes: List[ChangeProvenanceItem] = []
                for item in raw_unexplained_changes:
                    if not isinstance(item, dict):
                        return _parse_error(raw_response, "schema_error", "Malformed unexplained change entry", "unexplained change entries must be objects")
                    paths = item.get("paths", [])
                    change_group = str(item.get("change_group", "")).strip()
                    why_unexplained = str(item.get("why_unexplained", "")).strip()
                    if not isinstance(paths, list) or not paths or any(not isinstance(path, str) or not path.strip() for path in paths) or not change_group or not why_unexplained:
                        return _parse_error(raw_response, "schema_error", "Malformed unexplained change entry", "each unexplained change requires paths, change_group, and why_unexplained")
                    unexplained_changes.append(
                        ChangeProvenanceItem(
                            paths=[path.strip() for path in paths],
                            change_group=change_group,
                            why_unexplained=why_unexplained,
                        )
                    )

                raw_requirement_coverage = parsed.get("requirement_coverage", [])
                if not isinstance(raw_requirement_coverage, list):
                    return _parse_error(
                        raw_response,
                        "schema_error",
                        "Malformed validator schema: requirement_coverage must be a list",
                        f"requirement_coverage must be a list, got {type(raw_requirement_coverage).__name__}",
                    )
                requirement_coverage: List[RequirementCoverageEntry] = []
                seen_requirement_ids: set[str] = set()
                valid_coverage_statuses = {"VERIFIED", "VIOLATED", "IRRELEVANT", "UNVERIFIED"}
                for item in raw_requirement_coverage:
                    if not isinstance(item, dict):
                        return _parse_error(
                            raw_response,
                            "schema_error",
                            "Malformed requirement coverage entry",
                            "requirement coverage entries must be objects",
                        )
                    requirement_id = str(item.get("requirement_id", "")).strip()
                    status = str(item.get("status", "")).strip().upper()
                    evidence = str(item.get("evidence", "")).strip()
                    if not requirement_id or status not in valid_coverage_statuses or not evidence:
                        return _parse_error(
                            raw_response,
                            "schema_error",
                            "Malformed requirement coverage entry",
                            "each coverage entry requires a unique requirement_id, valid status, and non-empty evidence",
                        )
                    if requirement_id in seen_requirement_ids:
                        return _parse_error(
                            raw_response,
                            "schema_error",
                            "Duplicate requirement coverage entry",
                            f"requirement_id {requirement_id} appears more than once",
                        )
                    seen_requirement_ids.add(requirement_id)
                    requirement_coverage.append(
                        RequirementCoverageEntry(
                            requirement_id=requirement_id,
                            status=status,
                            evidence=evidence,
                        )
                    )

                raw_recovery = parsed.get("evidence_recovery", [])
                if not isinstance(raw_recovery, list) or len(raw_recovery) > ADVERSARIAL_EVIDENCE_RECOVERY_BUDGET:
                    return _parse_error(raw_response, "schema_error", "Malformed evidence recovery report", f"evidence_recovery must contain at most {ADVERSARIAL_EVIDENCE_RECOVERY_BUDGET} entries")
                evidence_recovery: List[EvidenceRecoveryEntry] = []
                for item in raw_recovery:
                    if not isinstance(item, dict):
                        return _parse_error(raw_response, "schema_error", "Malformed evidence recovery entry", "evidence recovery entries must be objects")
                    path = str(item.get("path", "")).strip()
                    source = str(item.get("source", "")).strip()
                    status = str(item.get("status", "")).strip().upper()
                    evidence = str(item.get("evidence", "")).strip()
                    requirement_ids = item.get("requirement_ids", [])
                    if not path or not source or status not in {"RECOVERED", "UNAVAILABLE", "IRRELEVANT"} or not evidence or not isinstance(requirement_ids, list) or any(not isinstance(value, str) or not value.strip() for value in requirement_ids):
                        return _parse_error(raw_response, "schema_error", "Malformed evidence recovery entry", "each recovery entry requires path, supported source, status, evidence, and requirement_ids")
                    evidence_recovery.append(EvidenceRecoveryEntry(path=path, source=source, status=status, evidence=evidence, requirement_ids=[value.strip() for value in requirement_ids]))

                raw_gaps = parsed.get("decision_critical_evidence_gaps", [])
                if not isinstance(raw_gaps, list):
                    return _parse_error(raw_response, "schema_error", "Malformed decision-critical evidence gaps", "decision_critical_evidence_gaps must be a list")
                decision_gaps: List[DecisionCriticalEvidenceGap] = []
                for item in raw_gaps:
                    if not isinstance(item, dict):
                        return _parse_error(raw_response, "schema_error", "Malformed decision-critical evidence gap", "evidence gap entries must be objects")
                    requirement_id = str(item.get("requirement_id", "")).strip()
                    evidence_needed = str(item.get("evidence_needed", "")).strip()
                    attempts = item.get("recovery_attempts", [])
                    if not requirement_id or not evidence_needed or not isinstance(attempts, list) or not attempts or any(not isinstance(value, str) or not value.strip() for value in attempts):
                        return _parse_error(raw_response, "schema_error", "Malformed decision-critical evidence gap", "each gap requires one requirement, concrete evidence, and attempted recovery sources")
                    decision_gaps.append(DecisionCriticalEvidenceGap(requirement_id=requirement_id, evidence_needed=evidence_needed, recovery_attempts=[value.strip() for value in attempts]))

                raw_findings = parsed.get("findings")
                findings: List[AdversarialValidationFinding] = []
                unverified_suspicion = False

                if raw_findings is not None:
                    if not isinstance(raw_findings, list):
                        return _parse_error(
                            raw_response,
                            "schema_error",
                            "Malformed validator schema: findings must be a list",
                            f"findings must be a list, got {type(raw_findings).__name__}",
                        )

                    for item in raw_findings:
                        if not isinstance(item, dict):
                            # Non-dict item in findings list is malformed schema
                            return _parse_error(
                                raw_response,
                                "schema_error",
                                "Malformed finding entry in validator response",
                                f"finding entry must be an object, got {type(item).__name__}",
                            )

                        if not item:
                            # Empty dict in findings is malformed
                            return _parse_error(
                                raw_response,
                                "schema_error",
                                "Malformed empty finding entry in validator response",
                                "finding entry must not be empty",
                            )

                        classification = str(item.get("evidence_classification", "")).strip().upper()
                        if classification not in {"DEMONSTRATED", "UNVERIFIED"}:
                            return _parse_error(
                                raw_response,
                                "schema_error",
                                "Reviewer finding lacks a valid evidence classification",
                                "evidence_classification must be DEMONSTRATED or UNVERIFIED",
                            )

                        if classification == "UNVERIFIED":
                            unverified_suspicion = True
                            continue

                        required_fields = {
                            "finding_identity": str(item.get("finding_identity", "")).strip(),
                            "correction_identity": str(item.get("correction_identity", "")).strip(),
                            "violated_requirement": str(item.get("violated_requirement", "")).strip(),
                            "reachability": str(item.get("reachability", "")).strip(),
                            "required_behavior": str(item.get("required_behavior", "")).strip(),
                            "actual_behavior": str(item.get("actual_behavior", "")).strip(),
                            "evidence": str(item.get("evidence", "")).strip(),
                            "counterexample": str(item.get("counterexample", "")).strip(),
                            "anchor_path": str(item.get("anchor_path", "")).strip(),
                        }
                        raw_finding_requirement_ids = item.get("requirement_ids", [])
                        if not isinstance(raw_finding_requirement_ids, list):
                            return _parse_error(raw_response, "schema_error", "Malformed finding requirement IDs", "requirement_ids must be a list")
                        finding_requirement_ids = list(
                            dict.fromkeys(
                                requirement_id
                                for requirement_id in [
                                    str(item.get("requirement_id", "")).strip(),
                                    *(str(value).strip() for value in raw_finding_requirement_ids),
                                ]
                                if requirement_id
                            )
                        )
                        if not finding_requirement_ids:
                            required_fields["requirement_ids"] = ""
                        missing_fields = [name for name, value in required_fields.items() if not value]
                        if missing_fields:
                            return _parse_error(
                                raw_response,
                                "schema_error",
                                "Reviewer reported a blocking defect without demonstrated reachability evidence",
                                f"DEMONSTRATED finding is missing: {', '.join(missing_fields)}",
                            )

                        req = required_fields["violated_requirement"]
                        ce = required_fields["counterexample"]
                        gap = str(item.get("test_gap", "")).strip() or "Existing tests do not assert this condition"
                        scen = str(item.get("suggested_regression_scenario", "")).strip() or "Add regression test covering counterexample"

                        findings.append(
                            AdversarialValidationFinding(
                                requirement_id=finding_requirement_ids[0],
                                requirement_ids=finding_requirement_ids,
                                finding_identity=required_fields["finding_identity"],
                                correction_identity=required_fields["correction_identity"],
                                violated_requirement=req,
                                reachability=required_fields["reachability"],
                                required_behavior=required_fields["required_behavior"],
                                actual_behavior=required_fields["actual_behavior"],
                                evidence=required_fields["evidence"],
                                evidence_classification=classification,
                                counterexample=ce,
                                test_gap=gap,
                                suggested_regression_scenario=scen,
                                anchor_path=required_fields["anchor_path"],
                                anchor_line=_optional_positive_int(item.get("anchor_line")),
                                anchor_side=str(item.get("anchor_side", "RIGHT")).strip().upper(),
                                anchor_start_line=_optional_positive_int(item.get("anchor_start_line")),
                            )
                        )

                # A finding is a concrete counterexample, not a requirement
                # perspective. Compact duplicate perspectives while retaining
                # independent findings whose observable path or fix differs.
                compacted_findings: List[AdversarialValidationFinding] = []
                findings_by_counterexample: Dict[tuple[str, ...], AdversarialValidationFinding] = {}
                for finding in findings:
                    # Structured identities, rather than reviewer prose, decide
                    # grouping. The correction identity prevents one root label
                    # from collapsing independently actionable fixes.
                    identity = (finding.finding_identity, finding.correction_identity)
                    existing = findings_by_counterexample.get(identity)
                    if existing is None:
                        findings_by_counterexample[identity] = finding
                        compacted_findings.append(finding)
                        continue
                    existing.requirement_ids = list(dict.fromkeys([*existing.all_requirement_ids, *finding.all_requirement_ids]))
                    for field_name in (
                        "violated_requirement",
                        "reachability",
                        "required_behavior",
                        "actual_behavior",
                        "evidence",
                        "counterexample",
                        "test_gap",
                        "suggested_regression_scenario",
                    ):
                        merged_values = list(dict.fromkeys(value for value in [getattr(existing, field_name), getattr(finding, field_name)] if value))
                        setattr(existing, field_name, "\n\n".join(merged_values))
                findings = compacted_findings

                if unverified_suspicion and not findings:
                    for entry in requirement_coverage:
                        if entry.status == "VIOLATED":
                            entry.status = "UNVERIFIED"

                # Determine result - enforce consistency and fail-closed
                has_open_test_oracle_gaps = any(gap.status == "OPEN" for gap in test_oracle_gaps)
                if raw_result == "PASS":
                    # Valid concrete findings outrank the contradictory top-level
                    # label. Malformed findings have already failed schema parsing.
                    result_val = "NEEDS_FIX" if findings else "INCONCLUSIVE" if unverified_suspicion else "NEEDS_TESTS" if has_open_test_oracle_gaps else "PASS"
                elif raw_result == "NEEDS_FIX":
                    if not findings:
                        if unverified_suspicion:
                            result_val = "INCONCLUSIVE"
                        else:
                            # A bare NEEDS_FIX remains malformed and fails closed.
                            return _parse_error(
                                raw_response,
                                "schema_error",
                                "Reviewer claimed NEEDS_FIX without supplying demonstrated behavioral counterexamples",
                                "NEEDS_FIX requires at least one DEMONSTRATED finding",
                            )
                    else:
                        result_val = "NEEDS_FIX"
                elif raw_result == "NEEDS_TESTS":
                    if findings:
                        result_val = "NEEDS_FIX"
                    elif not has_open_test_oracle_gaps:
                        return _parse_error(
                            raw_response,
                            "schema_error",
                            "Reviewer claimed NEEDS_TESTS without an open material test-oracle gap",
                            "NEEDS_TESTS requires at least one OPEN test_oracle_gaps entry",
                        )
                    else:
                        result_val = "NEEDS_TESTS"
                elif raw_result in ("INCONCLUSIVE", "BLOCKED"):
                    # A proven counterexample outranks uncertainty or an
                    # infrastructure diagnostic. Never discard concrete findings.
                    result_val = "NEEDS_FIX" if findings else raw_result
                else:
                    return _parse_error(
                        raw_response,
                        "schema_error",
                        f"Unrecognized validator result: {raw_result or '<missing>'}",
                        "result must be PASS, NEEDS_FIX, NEEDS_TESTS, INCONCLUSIVE, or BLOCKED",
                    )

                thread_dispositions = _extract_thread_dispositions(parsed.get("thread_dispositions", []))

                return AdversarialValidationResult(
                    result=result_val,
                    summary=summary or ("Validation passed" if result_val == "PASS" else f"Validation status: {result_val}"),
                    findings=findings,
                    raw_response=raw_response,
                    dynamic_check_requested=dynamic_check,
                    requirement_coverage=requirement_coverage,
                    evidence_recovery=evidence_recovery,
                    decision_critical_evidence_gaps=decision_gaps,
                    specification_gaps=specification_gaps,
                    test_oracle_gaps=test_oracle_gaps,
                    thread_dispositions=thread_dispositions,
                    unexplained_changes=unexplained_changes,
                )
            else:
                return _parse_error(
                    raw_response,
                    "schema_error",
                    "Malformed validator schema: JSON root must be an object",
                    f"JSON root must be an object, got {type(parsed).__name__}",
                )
        except json.JSONDecodeError as exc:
            json_failure_reason = f"{exc.msg} at line {exc.lineno}, column {exc.colno}"
        except (TypeError, ValueError) as exc:
            # Schema normalization belongs to this response boundary.  Invalid
            # anchors must become a structured ERROR rather than escaping into
            # generic PR-processing exception handling.
            return _parse_error(
                raw_response,
                "schema_error",
                "Malformed validator review anchor",
                str(exc),
            )

    # 2. Fallback: Parse structured text markers
    violated_req = ""
    counterexample = ""
    test_gap = ""
    suggested_reg = ""

    for line in cleaned_response.splitlines():
        line_str = line.strip()
        if line_str.upper().startswith("VIOLATED_REQUIREMENT:") or line_str.upper().startswith("VIOLATED REQUIREMENT:"):
            violated_req = line.split(":", 1)[1].strip()
        elif line_str.upper().startswith("COUNTEREXAMPLE:"):
            counterexample = line.split(":", 1)[1].strip()
        elif line_str.upper().startswith("TEST_GAP:") or line_str.upper().startswith("WHY_TESTS_PASS:"):
            test_gap = line.split(":", 1)[1].strip()
        elif line_str.upper().startswith("SUGGESTED_REGRESSION_SCENARIO:") or line_str.upper().startswith("SUGGESTED_REGRESSION:"):
            suggested_reg = line.split(":", 1)[1].strip()

    has_text_findings = bool(violated_req or counterexample or test_gap or suggested_reg)

    if re.search(r"^\s*RESULT\s*:\s*NEEDS_FIX", cleaned_response, re.MULTILINE | re.IGNORECASE):
        return _parse_error(
            raw_response,
            "schema_error",
            "Legacy text finding cannot demonstrate the required reachable counterexample evidence",
            "blocking findings must use the structured JSON evidence schema",
        )

    if re.search(r"^\s*RESULT\s*:\s*PASS", cleaned_response, re.MULTILINE | re.IGNORECASE):
        if has_text_findings:
            return _parse_error(
                raw_response,
                "schema_error",
                "Legacy text finding cannot demonstrate the required reachable counterexample evidence",
                "blocking findings must use the structured JSON evidence schema",
            )
        return AdversarialValidationResult(
            result="PASS",
            summary="Validation passed",
            findings=[],
            raw_response=raw_response,
        )

    if re.search(r"^\s*RESULT\s*:\s*INCONCLUSIVE", cleaned_response, re.MULTILINE | re.IGNORECASE):
        return AdversarialValidationResult(
            result="INCONCLUSIVE",
            summary="Validation inconclusive",
            findings=[],
            raw_response=raw_response,
        )

    if re.search(r"^\s*RESULT\s*:\s*BLOCKED", cleaned_response, re.MULTILINE | re.IGNORECASE):
        return AdversarialValidationResult(
            result="BLOCKED",
            summary="Validation blocked by infrastructure or evidence failure",
            raw_response=raw_response,
        )

    if json_failure_reason:
        return _parse_error(
            raw_response,
            "json_parse_error",
            f"Invalid JSON in validator response: {json_failure_reason}",
            json_failure_reason,
        )

    return _parse_error(
        raw_response,
        "unrecognized_format",
        "Unparseable validator response; validation incomplete",
        "response contained neither a JSON object nor a recognized RESULT marker",
    )


def _same_test_oracle_gap_scope(left: TestOracleGap, right: TestOracleGap) -> bool:
    """Return whether two lifecycle entries describe the same missing oracle."""
    return left.gap_id == right.gap_id and left.requirement_id == right.requirement_id


def _reconcile_test_oracle_gap_lifecycle(
    result: AdversarialValidationResult,
    stored_session: Optional[ReviewerSession],
    head_sha: str,
    addressed_gap_evidence: Optional[dict[str, str]] = None,
) -> AdversarialValidationResult:
    """Enforce bounded initial discovery and deterministic rereview convergence."""
    if stored_session is None:
        for gap in result.test_oracle_gaps:
            gap.discovery_phase = "INITIAL"
            gap.rereview_exception_reason = "NONE"
            gap.rereview_exception_evidence = ""
            gap.status = "OPEN"
            gap.resolution_evidence = ""
        return result

    # Reconciliation is speculative until deterministic coverage/provenance
    # checks establish an authoritative lifecycle result. Never mutate the
    # registry-owned checkpoint objects during that speculative phase.
    prior_by_id = {gap.gap_id: replace(gap) for gap in stored_session.test_oracle_gaps}
    current_by_id = {gap.gap_id: gap for gap in result.test_oracle_gaps}
    addressed_gap_evidence = addressed_gap_evidence or {}
    reconciled: List[TestOracleGap] = []
    rejected_new_ids: List[str] = []

    for gap_id, prior in prior_by_id.items():
        current = current_by_id.pop(gap_id, None)
        accepted_head = prior.resolution_head_sha or stored_session.last_head_sha
        same_head = accepted_head == head_sha
        current_matches = current is not None and _same_test_oracle_gap_scope(prior, current)
        if prior.status in {"RESOLVED", "INVALID"} and not same_head:
            prior.resolution_head_sha = accepted_head
            prior.historical_resolution_head_sha = accepted_head
            prior.historical_resolution_evidence = prior.resolution_evidence
            if gap_id in addressed_gap_evidence:
                prior.status = "RESOLVED"
                prior.resolution_evidence = addressed_gap_evidence[gap_id]
                prior.resolution_head_sha = head_sha
                reconciled.append(prior)
                continue
            if current_matches and current is not None and current.status in {"RESOLVED", "INVALID"}:
                prior.status = current.status
                prior.resolution_evidence = current.resolution_evidence
                prior.resolution_head_sha = head_sha
                reconciled.append(prior)
                continue
            if current_matches and current is not None and current.status == "OPEN":
                prior.status = "OPEN"
                prior.resolution_evidence = ""
                prior.resolution_head_sha = ""
                reconciled.append(prior)
                continue
            # The old closure remains historical evidence, but omission is not
            # affirmative proof that protection remains on a different head.
            result.result = "BLOCKED"
            result.diagnostic_category = "test_oracle_gap_current_head_evidence_missing"
            result.diagnostic_reason = f"Current-head protection was not adjudicated for {gap_id} at {head_sha}"
            reconciled.append(prior)
            continue
        if current is None or not current_matches:
            if prior.status == "OPEN" and gap_id in addressed_gap_evidence:
                prior.status = "RESOLVED"
                prior.resolution_evidence = addressed_gap_evidence[gap_id]
                prior.resolution_head_sha = head_sha
            reconciled.append(prior)
            continue
        if prior.status in {"RESOLVED", "INVALID"}:
            # An accepted closure remains reusable after unchanged-head
            # confirmation; omission and stale OPEN echoes cannot revoke it.
            reconciled.append(prior)
            continue
        current.discovery_phase = prior.discovery_phase
        current.rereview_exception_reason = prior.rereview_exception_reason
        current.rereview_exception_evidence = prior.rereview_exception_evidence
        # Descriptive prose may be paraphrased by the reviewer. Preserve the
        # original authoritative scope while applying only lifecycle evidence.
        if prior.status == "OPEN" and gap_id in addressed_gap_evidence:
            # A disposition adjudicates the persisted finding, whereas the gap
            # list is only the reviewer's lifecycle projection.  In particular,
            # an OPEN echo must not undo an independently proven disposition for
            # the exact thread represented by this gap.
            prior.status = "RESOLVED"
            prior.resolution_evidence = addressed_gap_evidence[gap_id]
            prior.resolution_head_sha = head_sha
        else:
            prior.status = current.status
            prior.resolution_evidence = current.resolution_evidence
            if current.status in {"RESOLVED", "INVALID"}:
                prior.resolution_head_sha = head_sha
        reconciled.append(prior)

    for gap in current_by_id.values():
        if gap.status != "OPEN" or gap.discovery_phase != "REREVIEW" or gap.rereview_exception_reason not in TEST_ORACLE_GAP_REREVIEW_EXCEPTIONS or not gap.rereview_exception_evidence:
            rejected_new_ids.append(gap.gap_id)
            continue
        reconciled.append(gap)

    result.test_oracle_gaps = reconciled
    if result.result not in {"ERROR", "INCONCLUSIVE", "BLOCKED"}:
        if result.findings:
            result.result = "NEEDS_FIX"
        elif result.open_test_oracle_gaps:
            result.result = "NEEDS_TESTS"
        elif result.result == "NEEDS_TESTS":
            result.result = "PASS"
    if rejected_new_ids:
        note = "Rereview discarded newly invented test-oracle gaps without a permitted corrective-diff or required-revalidation exception: " + ", ".join(sorted(rejected_new_ids))
        result.summary = f"{result.summary.rstrip()} {note}".strip()
    return result


def _addressed_test_oracle_gap_evidence(
    result: AdversarialValidationResult,
    claimed_review_threads: Sequence["ClaimedReviewThread"],
    recorded_gaps: Sequence[TestOracleGap] = (),
) -> dict[str, str]:
    """Map exact material-gap threads independently proven addressed this run."""
    dispositions = {disposition.thread_id: disposition for disposition in result.thread_dispositions if disposition.status == "ADDRESSED" and disposition.rationale and disposition.evidence}
    gaps_by_id = {gap.gap_id: gap for gap in (*recorded_gaps, *result.test_oracle_gaps)}
    evidence_by_gap: dict[str, str] = {}
    for thread in claimed_review_threads:
        disposition = dispositions.get(thread.thread_id)
        if disposition is None:
            continue
        gap_match = re.search(r"^Gap identity:\s*`(TOG-[^`]+)`\s*$", thread.original_finding, re.MULTILINE)
        requirement_match = re.search(r"^`(REQ-[^`]+)`:\s*\S", thread.original_finding, re.MULTILINE)
        current_gap = gaps_by_id.get(gap_match.group(1)) if gap_match else None
        if gap_match and requirement_match and current_gap is not None and current_gap.requirement_id == requirement_match.group(1):
            evidence_by_gap[gap_match.group(1)] = f"{disposition.rationale}\nEvidence: {disposition.evidence}"
    return evidence_by_gap


def _enforce_inconclusive_recovery_contract(
    result: AdversarialValidationResult,
    incomplete_requirement_ids: List[str],
) -> AdversarialValidationResult:
    """Reject a final INCONCLUSIVE verdict without scoped recovery evidence."""
    if result.result.strip().upper() != "INCONCLUSIVE" or result.dynamic_check_requested:
        return result

    missing_contract_parts: List[str] = []
    if not result.evidence_recovery:
        missing_contract_parts.append("bounded evidence-recovery attempts")
    if not result.decision_critical_evidence_gaps:
        missing_contract_parts.append("a decision-critical evidence gap")
    if missing_contract_parts:
        reason = f"INCONCLUSIVE requires {', '.join(missing_contract_parts)}"
        result.result = "ERROR"
        result.summary = f"Invalid validator response: {reason}"
        result.diagnostic_category = "inconclusive_without_exhausted_evidence_recovery"
        result.diagnostic_reason = reason
        return result

    incomplete_ids = set(incomplete_requirement_ids)
    recovery_ids = {requirement_id for entry in result.evidence_recovery for requirement_id in entry.requirement_ids}
    gap_ids = {gap.requirement_id for gap in result.decision_critical_evidence_gaps}
    requirements_without_recovery = sorted(incomplete_ids - recovery_ids)
    requirements_without_gap = sorted(incomplete_ids - gap_ids)
    gaps_for_verified_requirements = sorted(gap_ids - incomplete_ids)
    if requirements_without_recovery or requirements_without_gap or gaps_for_verified_requirements:
        scope_errors: List[str] = []
        if requirements_without_recovery:
            scope_errors.append(f"requirements without recovery attempts: {', '.join(requirements_without_recovery)}")
        if requirements_without_gap:
            scope_errors.append(f"requirements without decision-critical gaps: {', '.join(requirements_without_gap)}")
        if gaps_for_verified_requirements:
            scope_errors.append(f"gaps for already-decided requirements: {', '.join(gaps_for_verified_requirements)}")
        reason = "; ".join(scope_errors)
        result.result = "ERROR"
        result.summary = "Invalid validator response: INCONCLUSIVE evidence was not scoped to undecided requirements"
        result.diagnostic_category = "inconclusive_evidence_scope_mismatch"
        result.diagnostic_reason = reason
    return result


def _reconcile_same_head_recovered_evidence(
    result: AdversarialValidationResult,
    stored_session: Optional[ReviewerSession],
    head_sha: str,
) -> AdversarialValidationResult:
    """Retain successful file recovery when revalidating an unchanged head."""
    if stored_session is None or stored_session.evidence_head_sha != head_sha:
        return result
    current_resolved = {entry.path for entry in result.evidence_recovery if entry.status in {"RECOVERED", "IRRELEVANT"}}
    for persisted in stored_session.recovered_file_evidence:
        if persisted.path not in current_resolved:
            result.evidence_recovery.append(
                EvidenceRecoveryEntry(
                    path=persisted.path,
                    source=persisted.source,
                    status=persisted.status,
                    evidence=persisted.evidence,
                    requirement_ids=list(persisted.requirement_ids),
                )
            )
    return result


def _carry_forward_current_run_recovered_evidence(
    result: AdversarialValidationResult,
    recovered_evidence: List[EvidenceRecoveryEntry],
) -> AdversarialValidationResult:
    """Preserve successful recovery when a dynamic follow-up replaces the result."""
    resolved_paths = {entry.path for entry in result.evidence_recovery if entry.status in {"RECOVERED", "IRRELEVANT"}}
    for entry in recovered_evidence:
        if entry.path not in resolved_paths:
            result.evidence_recovery.append(replace(entry, requirement_ids=list(entry.requirement_ids)))
    return result


def _remaining_unverified_paths(result: AdversarialValidationResult, context: AdversarialValidationContext) -> List[str]:
    """Return exact paths not discharged by valid reviewer accounting."""
    accounted = {entry.path for entry in result.evidence_recovery if entry.status in {"RECOVERED", "IRRELEVANT"}}
    return [path for path in context.unverified_files if path not in accounted]


def _complete_changed_file_evidence(
    result: AdversarialValidationResult,
    context: AdversarialValidationContext,
    backend_manager: BackendManager,
    requirement_manifest: str,
    head_sha: str,
) -> AdversarialValidationResult:
    """Perform the one controller-driven, same-session coverage continuation."""
    unresolved = _remaining_unverified_paths(result, context)
    if result.result != "PASS" or result.findings or result.open_test_oracle_gaps or not unresolved:
        return result
    evidence_by_path = {entry.path: entry for entry in context.controller_file_evidence}
    retrievals = []
    for path in unresolved:
        evidence = evidence_by_path.get(path)
        retrievals.append(
            {
                "path": path,
                "status": "COMPLETE" if evidence is not None and evidence.is_complete else "UNAVAILABLE",
                "source": "GitHub REST pull-request files representation",
                "evidence": evidence.patch if evidence is not None else "Authoritative per-path retrieval failed or was incomplete",
            }
        )
    session_id = getattr(backend_manager, "_last_session_id", None)
    if not isinstance(session_id, str) or not session_id:
        return AdversarialValidationResult(
            result="ERROR",
            summary="Changed-file evidence completion could not continue the reviewer session",
            diagnostic_category="changed_file_completion_session_unavailable",
            diagnostic_reason=f"Unresolved paths: {', '.join(unresolved)}",
        )
    prompt = render_prompt(
        "pr.adversarial_validation_evidence_completion",
        validation_snapshot=context.validation_snapshot,
        head_sha=head_sha,
        unresolved_paths=json.dumps(unresolved, indent=2),
        requirement_manifest=requirement_manifest,
        prior_adjudication=result.raw_response or result.summary,
        controller_retrievals=json.dumps(retrievals, indent=2),
    )
    response = backend_manager.continue_session(session_id, prompt, is_noedit=True)
    completion = parse_adversarial_validation_response(response)
    _log_contextual_parse_diagnostics(completion, response, backend_manager, context.pr_number, "evidence_completion")
    supplied_complete = {item["path"] for item in retrievals if item["status"] == "COMPLETE"}
    invalid_recovered = [entry.path for entry in completion.evidence_recovery if entry.status == "RECOVERED" and entry.path not in supplied_complete]
    accounted = {entry.path for entry in completion.evidence_recovery if entry.path in unresolved}
    if invalid_recovered or accounted != set(unresolved):
        return AdversarialValidationResult(
            result="ERROR",
            summary="Invalid or incomplete changed-file evidence completion response",
            diagnostic_category="invalid_changed_file_completion",
            diagnostic_reason=f"Invalid recovered paths: {invalid_recovered}; unaccounted paths: {sorted(set(unresolved) - accounted)}",
            evidence_recovery=completion.evidence_recovery,
            requirement_coverage=completion.requirement_coverage,
            test_oracle_gaps=completion.test_oracle_gaps,
            thread_dispositions=completion.thread_dispositions,
        )
    return completion


def _apply_coverage_and_verdict_precedence(
    result: AdversarialValidationResult,
    context: AdversarialValidationContext,
) -> AdversarialValidationResult:
    """Apply deterministic finding-first and complete-coverage verdict rules."""
    if result.result == "ERROR" and result.diagnostic_category:
        return result
    expected_requirement_ids = {requirement.requirement_id for requirement in context.issue_requirements}
    recovery_requirement_ids = {requirement_id for entry in result.evidence_recovery for requirement_id in entry.requirement_ids}
    evidence_gap_requirement_ids = {gap.requirement_id for gap in result.decision_critical_evidence_gaps}
    unknown_evidence_requirement_ids = sorted((recovery_requirement_ids | evidence_gap_requirement_ids) - expected_requirement_ids)
    if unknown_evidence_requirement_ids:
        result.result = "ERROR"
        result.summary = "Invalid validator response: evidence recovery referenced unknown stable requirement IDs"
        result.diagnostic_category = "unknown_evidence_recovery_requirement_id"
        result.diagnostic_reason = f"Evidence recovery references IDs outside the deterministic manifest: {', '.join(unknown_evidence_requirement_ids)}"
        return result
    recovered_paths = {entry.path for entry in result.evidence_recovery if entry.status in {"RECOVERED", "IRRELEVANT"} and entry.path in context.unverified_files}
    remaining_unverified_files = [path for path in context.unverified_files if path not in recovered_paths]
    coverage_by_id = {entry.requirement_id: entry for entry in result.requirement_coverage}
    unknown_requirement_ids = sorted(coverage_by_id.keys() - expected_requirement_ids)
    missing_requirement_ids = sorted(expected_requirement_ids - coverage_by_id.keys())
    unresolved_requirement_ids = sorted(requirement_id for requirement_id in expected_requirement_ids & coverage_by_id.keys() if coverage_by_id[requirement_id].status not in {"VERIFIED", "IRRELEVANT"})
    incomplete_requirement_ids = missing_requirement_ids + unresolved_requirement_ids
    violated_requirement_ids = sorted(requirement_id for requirement_id in expected_requirement_ids & coverage_by_id.keys() if coverage_by_id[requirement_id].status == "VIOLATED")
    finding_requirement_ids = {requirement_id for finding in result.findings for requirement_id in finding.all_requirement_ids}
    unknown_finding_requirement_ids = sorted(finding_requirement_ids - expected_requirement_ids)
    gap_requirement_ids = {gap.requirement_id for gap in result.test_oracle_gaps}
    unknown_gap_requirement_ids = sorted(gap_requirement_ids - expected_requirement_ids)
    invalid_gap_anchors = sorted({gap.anchor_path for gap in result.open_test_oracle_gaps if gap.anchor_path not in context.all_changed_files})

    unavailable_requirement_ids = {requirement_id for entry in result.evidence_recovery if entry.status == "UNAVAILABLE" and entry.path in remaining_unverified_files for requirement_id in entry.requirement_ids}
    incompatible_verified_ids = sorted(requirement_id for requirement_id in unavailable_requirement_ids if requirement_id in coverage_by_id and coverage_by_id[requirement_id].status in {"VERIFIED", "IRRELEVANT"})
    if incompatible_verified_ids:
        result.result = "ERROR"
        result.summary = "Invalid validator response: unavailable evidence was declared for already-decided requirements"
        result.diagnostic_category = "requirement_evidence_claim_mismatch"
        result.diagnostic_reason = "Correctness-relevant changed-file evidence is unavailable while dependent requirements " f"are marked decided: {', '.join(incompatible_verified_ids)}"
        return result

    if unknown_finding_requirement_ids:
        reason = f"Findings reference IDs outside the deterministic manifest: {', '.join(unknown_finding_requirement_ids)}"
        result.result = "ERROR"
        result.summary = "Invalid validator response: findings referenced unknown stable requirement IDs"
        result.findings = []
        result.diagnostic_category = "unknown_finding_requirement_id"
        result.diagnostic_reason = reason
        return result

    if unknown_gap_requirement_ids:
        reason = f"Test-oracle gaps reference IDs outside the deterministic manifest: {', '.join(unknown_gap_requirement_ids)}"
        result.result = "ERROR"
        result.summary = "Invalid validator response: test-oracle gaps referenced unknown stable requirement IDs"
        result.test_oracle_gaps = [gap for gap in result.test_oracle_gaps if gap.requirement_id in expected_requirement_ids]
        result.diagnostic_category = "unknown_test_oracle_gap_requirement_id"
        result.diagnostic_reason = reason
        return result

    # The stable ID is the model's only requirement reference.  Once that ID
    # has passed the fail-closed manifest check above, attach authoritative
    # display text locally instead of trusting an echoed copy from the model.
    _populate_test_oracle_gap_requirement_text(result.test_oracle_gaps, context.issue_requirements)

    if invalid_gap_anchors:
        reason = f"Open test-oracle gaps must anchor to changed files: {', '.join(invalid_gap_anchors)}"
        result.result = "ERROR"
        result.summary = "Invalid validator response: test-oracle gaps used invalid changed-file anchors"
        result.test_oracle_gaps = [gap for gap in result.test_oracle_gaps if gap.anchor_path in context.all_changed_files]
        result.diagnostic_category = "invalid_test_oracle_gap_anchor"
        result.diagnostic_reason = reason
        return result

    if unknown_requirement_ids and not result.findings:
        reason = f"Requirement coverage contains IDs outside the deterministic manifest: {', '.join(unknown_requirement_ids)}"
        result.result = "ERROR"
        result.summary = "Invalid validator response: requirement coverage referenced unknown stable IDs"
        result.diagnostic_category = "unknown_requirement_coverage_id"
        result.diagnostic_reason = reason
        return result

    if violated_requirement_ids and not result.findings:
        reason = f"VIOLATED requirement coverage lacks a concrete counterexample finding: {', '.join(violated_requirement_ids)}"
        result.result = "ERROR"
        result.summary = "Contradictory validator response: violated requirements were reported without concrete findings"
        result.diagnostic_category = "violated_requirement_without_finding"
        result.diagnostic_reason = reason
        return result

    unrepresented_violated_ids = sorted(set(violated_requirement_ids) - finding_requirement_ids)
    if unrepresented_violated_ids:
        result.result = "ERROR"
        result.summary = "Contradictory validator response: violated requirements lacked a matching concrete finding"
        result.diagnostic_category = "violated_requirement_without_matching_finding"
        result.diagnostic_reason = f"No finding references: {', '.join(unrepresented_violated_ids)}"
        return result

    if result.findings:
        result.unexplained_changes = []
        result.result = "NEEDS_FIX"
        incomplete_coverage: List[str] = []
        coverage_details: List[str] = []
        if remaining_unverified_files:
            incomplete_coverage.append(f"{len(remaining_unverified_files)} changed file(s)")
            coverage_details.append(f"changed files: {', '.join(remaining_unverified_files)}")
        if not expected_requirement_ids:
            incomplete_coverage.append("Issue requirement manifest was empty")
            coverage_details.append("Issue requirement manifest was empty")
        elif incomplete_requirement_ids:
            incomplete_coverage.append(f"{len(incomplete_requirement_ids)} Issue requirement(s)")
            coverage_details.append(f"Issue requirement IDs: {', '.join(incomplete_requirement_ids)}")
        if unknown_requirement_ids:
            incomplete_coverage.append(f"{len(unknown_requirement_ids)} unknown requirement ID(s)")
            coverage_details.append(f"unknown requirement IDs: {', '.join(unknown_requirement_ids)}")
        if incomplete_coverage:
            coverage_note = f"Review coverage also remains incomplete for {', '.join(incomplete_coverage)}."
            if coverage_note not in result.summary:
                result.summary = f"{result.summary.rstrip()} {coverage_note}".strip()
            result.diagnostic_category = result.diagnostic_category or "incomplete_evidence_coverage"
            result.diagnostic_reason = result.diagnostic_reason or "; ".join(coverage_details)
        return result

    if result.open_test_oracle_gaps and result.result in {"PASS", "NEEDS_TESTS"}:
        result.result = "NEEDS_TESTS"
        if remaining_unverified_files:
            result.diagnostic_category = "incomplete_evidence_coverage"
            result.diagnostic_reason = f"Material changed-file evidence was incomplete for: {', '.join(remaining_unverified_files)}"
        elif not expected_requirement_ids or incomplete_requirement_ids:
            result.diagnostic_category = "incomplete_requirement_coverage"
            result.diagnostic_reason = "The deterministic Issue requirement manifest was empty" if not expected_requirement_ids else f"Material Issue requirement IDs remain unverified: {', '.join(incomplete_requirement_ids)}"

    if not remaining_unverified_files and expected_requirement_ids and not incomplete_requirement_ids and result.unexplained_changes:
        result.result = "INCONCLUSIVE"
        result.diagnostic_category = "change_provenance_clarification"
        result.diagnostic_reason = "Material changed-file purpose or provenance requires implementer clarification"
        # Provenance clarification is an orthogonal merge gate after all Issue
        # requirements and file evidence are complete, not an evidence-recovery
        # failure for an undecidable requirement.
        return result

    if result.unexplained_changes:
        # A provenance question is only authoritative after the Issue contract
        # itself is completely adjudicated. Other missing evidence retains its
        # existing diagnostic and must not be mislabeled as implementer provenance.
        result.unexplained_changes = []

    if result.result.strip().upper() == "PASS" and remaining_unverified_files:
        reason = f"Material changed-file evidence was incomplete after bounded recovery for: {', '.join(remaining_unverified_files)}"
        result.result = "ERROR"
        result.summary = "Invalid validator response: PASS conflicts with unresolved changed-file evidence"
        result.diagnostic_category = "pass_with_unresolved_changed_file_evidence"
        result.diagnostic_reason = reason
        return result

    if result.result.strip().upper() == "PASS" and (not expected_requirement_ids or incomplete_requirement_ids):
        if not expected_requirement_ids:
            reason = "The deterministic Issue requirement manifest was empty"
        else:
            reason = f"Material Issue requirement IDs remain unverified: {', '.join(incomplete_requirement_ids)}"
        result.result = "ERROR"
        result.summary = "Invalid validator response: PASS conflicts with incomplete Issue requirement coverage"
        result.diagnostic_category = "pass_with_incomplete_requirement_coverage"
        result.diagnostic_reason = reason
        return result
    return _enforce_inconclusive_recovery_contract(result, incomplete_requirement_ids)


def run_adversarial_validation(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    github_client: Optional[Any] = None,
    backend_manager: Optional[BackendManager] = None,
    session_registry: Optional[ReviewerSessionRegistry] = None,
    claimed_review_threads_section: Optional[str] = None,
    claimed_review_threads: Sequence["ClaimedReviewThread"] = (),
    execution_cwd: Optional[str] = None,
    defer_session_persistence: bool = False,
    ci_status: Optional["GitHubActionsStatusResult"] = None,
    refresh_ci_status: Optional[Callable[[], "GitHubActionsStatusResult"]] = None,
) -> AdversarialValidationResult:
    """Run strong-model adversarial validation on a green PR.

    Treats the issue specification & acceptance criteria as the test oracle,
    inspects the PR diff and changed tests, and attempts to falsify the implementation.

    Fail-closed protections:
    - If issue context cannot be retrieved (missing oracle), blocks merge (BLOCKED).
    - If no strong backend is available, blocks merge (BLOCKED).
    - If a dynamic check's exact execution revision cannot be proved, blocks merge.
    - If dynamic check passes, re-queries reviewer with test execution result for final decision.

    Args:
        repo_name: Repository name ('owner/repo')
        pr_data: Pull request metadata dictionary
        config: AutomationConfig instance
        github_client: Optional GitHubClient instance
        backend_manager: Optional BackendManager (defaults to strong model manager)

    Returns:
        AdversarialValidationResult indicating validation outcome
    """
    pr_number = pr_data.get("number", 0)
    head_sha = str((pr_data.get("head") or {}).get("sha") or pr_data.get("head_sha") or "")

    def verify_execution_target(boundary: str) -> None:
        if execution_cwd is None:
            return
        target = Path(execution_cwd)
        verification = CommandExecutor.run_command(["git", "rev-parse", "HEAD"], cwd=str(target))
        actual = verification.stdout.strip().lower()
        if not target.is_dir() or not verification.success or not head_sha or actual != head_sha.lower():
            detail = actual or verification.stderr.strip() or "unavailable"
            raise RuntimeError(f"Adversarial validation execution target mismatch at {boundary}: expected {head_sha}, found {detail}")

    verify_execution_target("context collection")
    logger.info(f"Starting strong-model adversarial validation for PR #{pr_number}")
    get_trace_logger().log("Adversarial Validation", f"Validating PR #{pr_number} against specification", item_type="pr", item_number=pr_number)

    # 1. Build validation context
    context = build_adversarial_validation_context(repo_name, pr_data, config, github_client)

    # Oracle acquisition check: If no specification context exists, fail closed
    if not context.issue_context or not context.issue_context.strip():
        logger.warning(f"PR #{pr_number} has no linked issue specification / oracle. Blocking merge.")
        return AdversarialValidationResult(
            result="BLOCKED",
            summary=f"Oracle acquisition failed for PR #{pr_number}: no linked issue specification found to falsify against",
        )

    if context.evidence_retrieval_error:
        logger.warning(f"PR #{pr_number} evidence retrieval failed. Blocking merge: {context.evidence_retrieval_error}")
        return AdversarialValidationResult(
            result="BLOCKED",
            summary=context.evidence_retrieval_error,
            diagnostic_category="evidence_retrieval_failure",
            diagnostic_reason=context.evidence_retrieval_error,
        )

    if context.requirement_manifest_error:
        logger.warning(f"PR #{pr_number} has an invalid Issue requirement contract: {context.requirement_manifest_error}")
        return AdversarialValidationResult(
            result="BLOCKED",
            summary=context.requirement_manifest_error,
            diagnostic_category="invalid_requirement_contract",
            diagnostic_reason=context.requirement_manifest_error,
        )

    if context.requires_human_review:
        reason = "PR has more than 300 changed files; automated adversarial validation cannot authorize merge and human review is required"
        logger.warning(f"PR #{pr_number} exceeds the raw-diff coverage limit. Blocking automatic merge.")
        return AdversarialValidationResult(
            result="BLOCKED",
            summary=reason,
            diagnostic_category="human_review_required",
            diagnostic_reason=reason,
        )

    # Diff accessibility check: If PR diff could not be retrieved, fail closed
    if not context.pr_diff or not context.pr_diff.strip():
        logger.warning(f"PR #{pr_number} has no accessible diff. Blocking merge.")
        return AdversarialValidationResult(
            result="BLOCKED",
            summary=f"Diff retrieval failed for PR #{pr_number}: no PR diff available for adversarial validation",
        )

    # 2. Select strong backend manager
    if backend_manager is None:
        from .cli_helpers import create_adversarial_validation_backend_manager

        backend_manager = create_adversarial_validation_backend_manager()

    if backend_manager is None:
        logger.error("No strong adversarial validation backend configured or available. Blocking merge.")
        return AdversarialValidationResult(
            result="BLOCKED",
            summary="No strong adversarial validation backend configured or available",
        )

    # 3. Render adversarial validation prompt with complete manifests and
    # deterministic file-evidence coverage metadata.
    changed_tests_str = _format_path_manifest(context.changed_tests, "(No test files detected in diff)")
    changed_files_str = _format_path_manifest(context.all_changed_files, "(No changed files detected)")
    requirement_entries = "\n".join(f"- {requirement.requirement_id}: {requirement.text}" for requirement in context.issue_requirements)
    requirement_manifest = f"Manifest mode: {context.requirement_manifest_mode}\n{requirement_entries}" if requirement_entries else f"Manifest mode: {context.requirement_manifest_mode}\n(Requirement manifest extraction failed; PASS is forbidden.)"
    if context.has_complete_file_coverage:
        coverage_status = "COMPLETE: every changed file has complete patch evidence."
    else:
        coverage_prefix = "INCOMPLETE: PASS is forbidden. Partial/unavailable file evidence:\n"
        coverage_status = coverage_prefix + _format_path_manifest(
            context.unverified_files,
            "(Unverified path metadata unavailable)",
        )
    registry = session_registry or ReviewerSessionRegistry()

    def manager_identity() -> tuple[str, str, str]:
        identity = backend_manager.get_current_backend_identity()
        if not isinstance(identity, tuple) or len(identity) != 3:
            return "", "", ""
        return str(identity[0]), str(identity[1]), str(identity[2])

    backend_name, backend_type, model_name = manager_identity()
    stored_session = registry.get(repo_name, pr_number, backend_name, backend_type, model_name) if backend_name else None
    if stored_session is not None:
        _populate_test_oracle_gap_requirement_text(stored_session.test_oracle_gaps, context.issue_requirements)
        if stored_session.evidence_head_sha == head_sha:
            persisted_resolved_paths = {entry.path for entry in stored_session.recovered_file_evidence if entry.status in {"RECOVERED", "IRRELEVANT"}}
            unresolved_paths = [path for path in context.unverified_files if path not in persisted_resolved_paths]
            if not unresolved_paths:
                coverage_status = "COMPLETE: every initially incomplete changed file was recovered or classified irrelevant on this exact head."
            else:
                coverage_prefix = "INCOMPLETE: PASS is forbidden. Partial/unavailable file evidence after same-head recovery:\n"
                coverage_status = coverage_prefix + _format_path_manifest(
                    unresolved_paths,
                    "(Unverified path metadata unavailable)",
                )
    lifecycle_session = stored_session if stored_session is not None and stored_session.last_head_sha else None
    if lifecycle_session is not None:
        review_policy = render_prompt(
            "pr.adversarial_validation_rereview",
            previous_head_sha=lifecycle_session.last_head_sha,
            current_head_sha=head_sha,
        )
    else:
        review_policy = render_prompt("pr.adversarial_validation_initial_review")

    prior_test_oracle_gaps = "(No material test-oracle gaps have been recorded for this PR.)"
    if lifecycle_session and lifecycle_session.test_oracle_gaps:
        prior_test_oracle_gaps = json.dumps(
            [gap.__dict__ for gap in lifecycle_session.test_oracle_gaps],
            indent=2,
            sort_keys=True,
        )

    prompt = render_prompt(
        "pr.adversarial_validation",
        review_policy=review_policy,
        repo_name=repo_name,
        pr_number=pr_number,
        pr_title=context.pr_title,
        pr_body=context.pr_body[: config.MAX_PROMPT_SIZE * 2],
        pr_diff=context.pr_diff,
        linked_issues_context=context.issue_context,
        changed_tests=changed_tests_str,
        changed_files=changed_files_str,
        coverage_status=coverage_status,
        requirement_manifest=requirement_manifest,
        claimed_review_threads=claimed_review_threads_section or "(No claimed-addressed review threads for this run.)",
        prior_test_oracle_gaps=prior_test_oracle_gaps,
        ci_execution_evidence=format_ci_execution_evidence(ci_status),
        adjacent_exploration_budget=ADVERSARIAL_ADJACENT_EXPLORATION_BUDGET,
        evidence_recovery_budget=ADVERSARIAL_EVIDENCE_RECOVERY_BUDGET,
    )

    # 4. Invoke the strong model
    with ProgressStage("Adversarial validation"):
        verify_execution_target("LLM invocation")
        if stored_session:
            response = backend_manager.continue_session(stored_session.session_id, prompt, is_noedit=True)
        else:
            response = run_llm_prompt(prompt, backend_manager=backend_manager, is_noedit=True)
    verify_execution_target("LLM completion")

    used_backend, used_type, used_model = manager_identity()
    provider_session_id = getattr(backend_manager, "_last_session_id", None)

    # 5. Parse response
    result = parse_adversarial_validation_response(response)
    _log_contextual_parse_diagnostics(result, response, backend_manager, pr_number, "initial")
    result = _reconcile_same_head_recovered_evidence(result, stored_session, head_sha)
    result = _reconcile_test_oracle_gap_lifecycle(
        result,
        lifecycle_session,
        head_sha,
        _addressed_test_oracle_gap_evidence(
            result,
            claimed_review_threads,
            lifecycle_session.test_oracle_gaps if lifecycle_session else (),
        ),
    )
    completion_was_required = bool(_remaining_unverified_paths(result, context) and result.result == "PASS" and not result.findings and not result.open_test_oracle_gaps)
    result = _complete_changed_file_evidence(result, context, backend_manager, requirement_manifest, head_sha)
    if completion_was_required and result.result != "ERROR":
        try:
            refresh_pr_data = pr_data
            if github_client is not None:
                live_pr_data = github_client.get_pull_request_metadata_strict(repo_name, pr_number)
                if not isinstance(live_pr_data, dict):
                    raise ValueError("live pull-request metadata was malformed")
                refresh_pr_data = live_pr_data
            refreshed = build_adversarial_validation_context(repo_name, refresh_pr_data, config, github_client)
        except Exception:
            refreshed = AdversarialValidationContext()
        if not context.validation_snapshot or refreshed.validation_snapshot != context.validation_snapshot:
            return AdversarialValidationResult(
                result="ERROR",
                summary="Changed-file evidence completion became stale and requires revalidation",
                diagnostic_category="validation_snapshot_stale",
                diagnostic_reason="PR head, base/change representation, or Issue Requirement manifest changed or could not be reconfirmed",
            )
    result = _apply_coverage_and_verdict_precedence(result, context)
    current_run_recovered_evidence = [replace(entry, requirement_ids=list(entry.requirement_ids)) for entry in result.evidence_recovery if entry.status in {"RECOVERED", "IRRELEVANT"} and entry.path in context.unverified_files]

    initial_thread_dispositions = result.thread_dispositions

    # 6. Dynamic validation on suspicion (if requested)
    if result.dynamic_check_requested and result.dynamic_check_requested.strip() and not result.needs_fix and not result.needs_tests:
        check_target = result.dynamic_check_requested.strip()
        logger.info(f"Adversarial reviewer requested dynamic validation check: {check_target}")
        # The initial reviewer round trip is an external-work boundary.  Never
        # use the carried observation to suppress execution unless production
        # can freshly prove it is still current.
        if refresh_ci_status is not None:
            ci_status = refresh_ci_status()
        target_error = validate_dynamic_check_target(check_target, Path(execution_cwd).resolve()) if execution_cwd else None
        correction_used = False
        if target_error:
            correction_used = True
            correction_ci_status = ci_status
            correction_ci_evidence = format_ci_execution_evidence(ci_status)
            correction_prompt = render_prompt(
                "pr.adversarial_validation_target_correction",
                invalid_target=check_target,
                target_diagnostic=target_error,
                ci_evidence=correction_ci_evidence,
            )
            correction_session_id = getattr(backend_manager, "_last_session_id", None)
            if not isinstance(correction_session_id, str) or not correction_session_id:
                result = AdversarialValidationResult(
                    result="ERROR",
                    summary="Reviewer target correction could not continue without an explicit session",
                    diagnostic_category="dynamic_check_target_protocol_error",
                    diagnostic_reason=target_error,
                )
            else:
                correction_response = backend_manager.continue_session(correction_session_id, correction_prompt, is_noedit=True)
                if refresh_ci_status is not None:
                    ci_status = refresh_ci_status()
                result = parse_adversarial_validation_response(correction_response)
                _log_contextual_parse_diagnostics(result, correction_response, backend_manager, pr_number, "target_correction")
                corrected_target = (result.dynamic_check_requested or "").strip()
                repeated_error = validate_dynamic_check_target(corrected_target, Path(execution_cwd).resolve()) if corrected_target and execution_cwd else None
                if repeated_error:
                    result = AdversarialValidationResult(
                        result="ERROR",
                        summary="Reviewer repeated an invalid dynamic-check target after the bounded correction step",
                        diagnostic_category="dynamic_check_target_protocol_error",
                        diagnostic_reason=repeated_error,
                    )
                check_target = corrected_target
                if result.result.strip().upper() == "PASS" and not _ci_decision_evidence_equivalent(correction_ci_status, ci_status):
                    return AdversarialValidationResult(
                        result="INCONCLUSIVE",
                        summary="Current exact-head CI evidence is unavailable or changed after target correction",
                        diagnostic_category="validator_evidence_unavailable",
                        diagnostic_reason=format_ci_execution_evidence(ci_status),
                    )
        if (check_target == "all" and canonical_pr_tests_succeeded(ci_status)) or (check_target != "all" and focused_ci_target_succeeded(ci_status, check_target)):
            logger.info("Reusing successful exact-head canonical PR Tests evidence instead of rerunning the suite")
            result.dynamic_check_requested = None
            result.summary = f"{result.summary} Reused successful exact-head {CANONICAL_PR_TESTS_WORKFLOW} evidence."
        elif check_target and result.dynamic_check_requested:
            try:
                test_res = run_exact_head_dynamic_check(config, check_target, head_sha, execution_cwd) if execution_cwd else run_exact_head_dynamic_check(config, check_target, head_sha)
                if test_res.target_selection_error:
                    if correction_used:
                        return AdversarialValidationResult(
                            result="ERROR",
                            summary="Reviewer repeated an unselectable dynamic-check target after the bounded correction step",
                            diagnostic_category="dynamic_check_target_protocol_error",
                            diagnostic_reason=test_res.target_selection_error,
                        )
                    correction_used = True
                    if refresh_ci_status is not None:
                        ci_status = refresh_ci_status()
                    correction_ci_status = ci_status
                    correction_ci_evidence = format_ci_execution_evidence(ci_status)
                    correction_prompt = render_prompt(
                        "pr.adversarial_validation_target_correction",
                        invalid_target=check_target,
                        target_diagnostic=test_res.target_selection_error,
                        ci_evidence=correction_ci_evidence,
                    )
                    correction_session_id = getattr(backend_manager, "_last_session_id", None)
                    if not isinstance(correction_session_id, str) or not correction_session_id:
                        return AdversarialValidationResult(
                            result="ERROR",
                            summary="Reviewer target correction could not continue without an explicit session",
                            diagnostic_category="dynamic_check_target_protocol_error",
                            diagnostic_reason=test_res.target_selection_error,
                        )
                    correction_response = backend_manager.continue_session(correction_session_id, correction_prompt, is_noedit=True)
                    if refresh_ci_status is not None:
                        ci_status = refresh_ci_status()
                    result = parse_adversarial_validation_response(correction_response)
                    _log_contextual_parse_diagnostics(result, correction_response, backend_manager, pr_number, "target_selection_correction")
                    corrected_target = (result.dynamic_check_requested or "").strip()
                    if not corrected_target:
                        if result.result.strip().upper() == "PASS" and not _ci_decision_evidence_equivalent(correction_ci_status, ci_status):
                            return AdversarialValidationResult(
                                result="INCONCLUSIVE",
                                summary="Current exact-head CI evidence is unavailable or changed after target correction",
                                diagnostic_category="validator_evidence_unavailable",
                                diagnostic_reason=format_ci_execution_evidence(ci_status),
                            )
                        return _apply_coverage_and_verdict_precedence(result, context)
                    repeated_error = validate_dynamic_check_target(corrected_target, Path(execution_cwd).resolve()) if execution_cwd else None
                    if repeated_error:
                        return AdversarialValidationResult(
                            result="ERROR",
                            summary="Reviewer repeated an invalid dynamic-check target after the bounded correction step",
                            diagnostic_category="dynamic_check_target_protocol_error",
                            diagnostic_reason=repeated_error,
                        )
                    check_target = corrected_target
                    if (check_target == "all" and canonical_pr_tests_succeeded(ci_status)) or (check_target != "all" and focused_ci_target_succeeded(ci_status, check_target)):
                        logger.info("Reusing refreshed exact-head CI evidence after dynamic-target correction")
                        result.dynamic_check_requested = None
                        result.summary = f"{result.summary} Reused successful exact-head {CANONICAL_PR_TESTS_WORKFLOW} evidence."
                        return _apply_coverage_and_verdict_precedence(result, context)
                    test_res = run_exact_head_dynamic_check(config, check_target, head_sha, execution_cwd) if execution_cwd else run_exact_head_dynamic_check(config, check_target, head_sha)
                    if test_res.target_selection_error:
                        return AdversarialValidationResult(
                            result="ERROR",
                            summary="Reviewer repeated an unselectable dynamic-check target after the bounded correction step",
                            diagnostic_category="dynamic_check_target_protocol_error",
                            diagnostic_reason=test_res.target_selection_error,
                        )
                if test_res.verification_error:
                    known_mismatch = test_res.executed_sha is not None
                    result.result = "ERROR" if known_mismatch else "INCONCLUSIVE"
                    result.summary = f"Dynamic validation evidence rejected: {test_res.verification_error}"
                    result.diagnostic_category = "dynamic_check_head_mismatch" if known_mismatch else "dynamic_check_head_unverifiable"
                    result.diagnostic_reason = test_res.verification_error
                    result.thread_dispositions = []
                    # The prior lifecycle remains authoritative: invalid execution
                    # evidence cannot resolve, invalidate, or recreate any gap.
                    if lifecycle_session is not None:
                        result.test_oracle_gaps = [replace(gap) for gap in lifecycle_session.test_oracle_gaps]
                    logger.error(result.summary) if known_mismatch else logger.warning(result.summary)
                else:
                    test_success = test_res.success
                    test_output = (test_res.output + "\n" + test_res.errors).strip()

                    # Preserve original counterexamples and findings in the follow-up
                    original_findings_blocks = []
                    for idx, f in enumerate(result.findings, start=1):
                        original_findings_blocks.append(f"Finding {idx}:\n" f"- Violated Requirement: {f.violated_requirement}\n" f"- Suspected Counterexample: {f.counterexample}\n" f"- Test Gap: {f.test_gap}\n" f"- Suggested Regression Scenario: {f.suggested_regression_scenario}\n")
                    original_findings_str = "\n".join(original_findings_blocks) if original_findings_blocks else "(No initial findings recorded)"

                    logger.info(f"Dynamic check executed on {check_target} (success={test_success}); querying reviewer with raw test output for final decision")
                    initial_thread_dispositions_str = "\n".join(f"- {d.thread_id}: {d.status} — {d.rationale}" for d in initial_thread_dispositions) if initial_thread_dispositions else "(No initial thread dispositions recorded)"
                    followup_prompt = render_prompt(
                        "pr.adversarial_validation_followup",
                        repo_name=repo_name,
                        pr_number=pr_number,
                        pr_title=context.pr_title,
                        check_target=check_target,
                        test_status="PASSED" if test_success else "FAILED",
                        test_success="True" if test_success else "False",
                        test_output=test_output[: config.MAX_PROMPT_SIZE * 2],
                        original_summary=result.summary,
                        original_findings=original_findings_str,
                        linked_issues_context=context.issue_context,
                        pr_diff=context.pr_diff,
                        requirement_manifest=requirement_manifest,
                        claimed_review_threads=claimed_review_threads_section or "(No claimed-addressed review threads for this run.)",
                        initial_thread_dispositions=initial_thread_dispositions_str,
                    )
                    with ProgressStage("Adversarial dynamic check follow-up"):
                        followup_session_id = getattr(backend_manager, "_last_session_id", None)
                        if isinstance(followup_session_id, str) and followup_session_id:
                            followup_response = backend_manager.continue_session(followup_session_id, followup_prompt, is_noedit=True)
                        else:
                            # Never guess an implicit last session. A provider that did not
                            # expose an ID cannot safely retain dynamic-check context.
                            raise RuntimeError("Reviewer provider did not return an explicit session ID for dynamic follow-up")
                    result = parse_adversarial_validation_response(followup_response)
                    _log_contextual_parse_diagnostics(result, followup_response, backend_manager, pr_number, "dynamic_check_followup")
                    result = _reconcile_same_head_recovered_evidence(result, stored_session, head_sha)
                    result = _carry_forward_current_run_recovered_evidence(result, current_run_recovered_evidence)
                    result = _reconcile_test_oracle_gap_lifecycle(
                        result,
                        lifecycle_session,
                        head_sha,
                        _addressed_test_oracle_gap_evidence(
                            result,
                            claimed_review_threads,
                            lifecycle_session.test_oracle_gaps if lifecycle_session else (),
                        ),
                    )
                result = _apply_coverage_and_verdict_precedence(result, context)
                if initial_thread_dispositions and not result.thread_dispositions:
                    # The follow-up prompt explicitly asks for a final disposition
                    # per claimed thread grounded in the dynamic-check evidence.
                    # Never resurrect the stale initial dispositions here: if the
                    # follow-up omitted them, that thread must fail closed to
                    # "no valid disposition" (stays unresolved) rather than reuse
                    # evidence the dynamic check may have since contradicted.
                    logger.warning(f"Dynamic-check follow-up for PR #{pr_number} returned no thread_dispositions; " f"{len(initial_thread_dispositions)} claimed thread(s) will not be resolved this run")
            except Exception as e:
                logger.warning(f"Failed to execute dynamic validation check '{check_target}': {e}")
                # Inability to complete a requested check must be treated as non-pass (fail-closed)
                result.result = "BLOCKED"
                result.summary = f"Dynamic validation check '{check_target}' could not be completed: {e}"
                if initial_thread_dispositions:
                    # Once dynamic re-adjudication starts, an initial disposition is
                    # provisional: it was explicitly not trusted enough to skip the
                    # dynamic check. If that check (or the follow-up call) fails
                    # before a final disposition is obtained, the initial one must
                    # not resolve any thread — clear it so every claimed thread
                    # stays unresolved (REQ-006, REQ-008).
                    logger.warning(f"Dynamic re-adjudication failed for PR #{pr_number}; discarding {len(initial_thread_dispositions)} initial thread disposition(s)")
                    result.thread_dispositions = []

    result = _apply_coverage_and_verdict_precedence(result, context)

    persisted_session_id = provider_session_id if isinstance(provider_session_id, str) and provider_session_id else stored_session.session_id if stored_session else ""
    if used_backend and persisted_session_id:
        incomplete_lifecycle_diagnostics = {"incomplete_evidence_coverage", "incomplete_requirement_coverage"}
        lifecycle_completed = result.result in {"PASS", "NEEDS_FIX", "NEEDS_TESTS"} and result.diagnostic_category not in incomplete_lifecycle_diagnostics
        # A proven exact-thread closure is authoritative for that gap even
        # when an unrelated gate (for example change provenance) keeps the
        # overall verdict inconclusive. Persist the semantic transition before
        # the caller projects the disposition to GitHub.
        prior_gaps_by_id = {gap.gap_id: gap for gap in lifecycle_session.test_oracle_gaps} if lifecycle_session else {}
        has_proven_gap_closure = any(
            prior_gaps_by_id.get(gap.gap_id) is not None
            and _same_test_oracle_gap_scope(prior_gaps_by_id[gap.gap_id], gap)
            and gap.status in {"RESOLVED", "INVALID"}
            and bool(gap.resolution_evidence)
            and gap.resolution_head_sha == head_sha
            and (prior_gaps_by_id[gap.gap_id].status == "OPEN" or (prior_gaps_by_id[gap.gap_id].resolution_head_sha or (lifecycle_session.last_head_sha if lifecycle_session else "")) != head_sha)
            for gap in result.test_oracle_gaps
        )
        persist_proven_closure = has_proven_gap_closure and result.result in {"PASS", "NEEDS_FIX", "NEEDS_TESTS", "INCONCLUSIVE", "BLOCKED"}
        persisted_head_sha = head_sha if lifecycle_completed or persist_proven_closure else lifecycle_session.last_head_sha if lifecycle_session else ""
        persisted_gaps = result.test_oracle_gaps if lifecycle_completed or persist_proven_closure else lifecycle_session.test_oracle_gaps if lifecycle_session else []
        checkpoint = ReviewerSession(
            repository=repo_name,
            pr_number=pr_number,
            backend_name=used_backend,
            backend_type=used_type,
            model_name=used_model,
            session_id=persisted_session_id,
            last_head_sha=persisted_head_sha,
            test_oracle_gaps=persisted_gaps,
            evidence_head_sha=head_sha,
            recovered_file_evidence=[
                RecoveredFileEvidence(
                    path=entry.path,
                    source=entry.source,
                    status=entry.status,
                    evidence=entry.evidence,
                    requirement_ids=list(entry.requirement_ids),
                )
                for entry in result.evidence_recovery
                if entry.status in {"RECOVERED", "IRRELEVANT"} and entry.path in context.unverified_files
            ],
        )
        result.reviewer_session_checkpoint = checkpoint
        result.reviewer_session_registry = registry
        if not defer_session_persistence:
            registry.save(checkpoint)

    get_trace_logger().log(
        "Adversarial Validation Result",
        f"Validation for PR #{pr_number}: {result.result}",
        item_type="pr",
        item_number=pr_number,
        details={"result": result.result, "findings_count": len(result.findings), "summary": result.summary},
    )

    return result
