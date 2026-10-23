"""Replay-safe publication of one controller-owned review-thread closure reply.

A receipt records GitHub effects and their recovery state. It never supplies an
independent review disposition or authority to resolve a finding.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from typing import Iterator, Optional

from .runtime_locks import ensure_lock_directory, file_lock, lock_path, runtime_root
from .util.gh_cache import GitHubClient, ReviewThread
from .util.github_request_outcome import normalize_api_origin
from .utils import is_same_github_login

CLOSURE_RECEIPT_MARKER = "<!-- auto-coder-review-thread-closure:v1 -->"
CLOSURE_RECEIPT_PREFIX = "<!-- auto-coder-review-thread-closure:"
_RECEIPT_DATA_PREFIX = "<!-- auto-coder-review-thread-closure-data: "
_RECEIPT_DATA_SUFFIX = " -->"
_RECEIPT_PHASES = frozenset({"PENDING", "RESOLVED", "ROLLED_BACK"})


class ClosureReceiptError(RuntimeError):
    """Receipt evidence is unavailable, ambiguous, malformed, or unconfirmed."""


@dataclass(frozen=True)
class ClosureReceipt:
    api_origin: str = "https://api.github.com"
    repository: str = ""
    pr_number: int = 0
    thread_id: str = ""
    root_comment_database_id: int = 0
    operation_key: str = ""
    validated_head_sha: str = ""
    phase: str = "PENDING"
    rationale: str = ""
    evidence: str = ""
    comment_id: Optional[int] = None


@dataclass(frozen=True)
class ReceiptObservation:
    thread: ReviewThread = field(default_factory=ReviewThread)
    authenticated_login: str = ""
    receipt: Optional[ClosureReceipt] = None


def _positive_id(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _validate_receipt(receipt: ClosureReceipt) -> None:
    required_strings = (receipt.api_origin, receipt.repository, receipt.thread_id, receipt.operation_key, receipt.validated_head_sha)
    if any(not isinstance(value, str) or not value.strip() for value in required_strings):
        raise ClosureReceiptError("Closure receipt contains missing or malformed operation identity")
    if not _positive_id(receipt.pr_number) or not _positive_id(receipt.root_comment_database_id):
        raise ClosureReceiptError("Closure receipt contains invalid PR or root identity")
    if receipt.comment_id is not None and not _positive_id(receipt.comment_id):
        raise ClosureReceiptError("Closure receipt contains invalid reply identity")
    if not isinstance(receipt.phase, str) or receipt.phase not in _RECEIPT_PHASES:
        raise ClosureReceiptError("Closure receipt contains an unknown phase")
    if not isinstance(receipt.rationale, str) or not isinstance(receipt.evidence, str):
        raise ClosureReceiptError("Closure receipt contains malformed explanation text")
    try:
        normalized = normalize_api_origin(receipt.api_origin)
    except (TypeError, ValueError) as exc:
        raise ClosureReceiptError("Closure receipt contains an invalid API origin") from exc
    if receipt.api_origin != normalized:
        raise ClosureReceiptError("Closure receipt API origin is not normalized")


def format_receipt(receipt: ClosureReceipt) -> str:
    """Render a versioned, exact envelope and its human-readable explanation."""
    _validate_receipt(receipt)
    payload = asdict(receipt)
    payload.pop("comment_id")
    # Escape HTML delimiters inside JSON so explanation text cannot end the
    # metadata comment and turn part of the envelope into visible markup.
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return "\n".join(
        (
            CLOSURE_RECEIPT_MARKER,
            f"{_RECEIPT_DATA_PREFIX}{data}{_RECEIPT_DATA_SUFFIX}",
            "### Auto-Coder independent validator verification",
            "",
            f"**Validated head:** `{receipt.validated_head_sha}`",
            f"**Closure state:** `{receipt.phase}`",
            "",
            "This reply records closure effects; independent validation remains required.",
            "",
            "**Rationale**",
            "",
            receipt.rationale or "(no rationale provided)",
            "",
            "**Evidence**",
            "",
            receipt.evidence or "(no evidence provided)",
        )
    )


def _parse_receipt_body(body: str, comment_id: Optional[int]) -> ClosureReceipt:
    lines = body.strip().splitlines()
    if len(lines) < 2 or lines[0] != CLOSURE_RECEIPT_MARKER or not lines[1].startswith(_RECEIPT_DATA_PREFIX) or not lines[1].endswith(_RECEIPT_DATA_SUFFIX):
        raise ClosureReceiptError("Owned closure receipt has a malformed envelope")
    encoded = lines[1][len(_RECEIPT_DATA_PREFIX) : -len(_RECEIPT_DATA_SUFFIX)]
    try:
        payload = json.loads(encoded)
        expected_fields = set(ClosureReceipt.__dataclass_fields__) - {"comment_id"}
        if not isinstance(payload, dict) or set(payload) != expected_fields:
            raise ValueError("receipt fields do not match the versioned schema")
        receipt = ClosureReceipt(**payload, comment_id=comment_id)
        _validate_receipt(receipt)
    except (TypeError, ValueError) as exc:
        raise ClosureReceiptError("Owned closure receipt has malformed metadata") from exc
    if not _positive_id(comment_id) or body.strip() != format_receipt(receipt).strip():
        raise ClosureReceiptError("Owned closure receipt body or reply identity is malformed")
    return receipt


def parse_owned_receipt(thread: ReviewThread, authenticated_login: str) -> Optional[ClosureReceipt]:
    """Read only a complete, unique receipt owned by the proven credential.

    Quoted markers and replies by another actor are ordinary discussion. An
    exact owned protocol prefix with an invalid envelope is unknown state.
    """
    if not isinstance(authenticated_login, str) or not authenticated_login.strip():
        raise ClosureReceiptError("Authenticated controller identity is unavailable")
    if thread.comments_truncated or not isinstance(thread.is_resolved, bool):
        raise ClosureReceiptError("Closure receipt thread observation is incomplete")
    owned = []
    for comment in thread.comments or []:
        body = comment.body or ""
        if is_same_github_login(comment.author_login, authenticated_login) and body.lstrip().startswith(CLOSURE_RECEIPT_PREFIX):
            owned.append(_parse_receipt_body(body, comment.database_id))
    if not owned:
        return None
    if len(owned) != 1:
        raise ClosureReceiptError("Multiple owned closure receipts exist for the same thread")
    root_id = thread.comments[0].database_id
    receipt = owned[0]
    if not _positive_id(root_id) or receipt.thread_id != thread.id or receipt.root_comment_database_id != root_id or receipt.comment_id == root_id:
        raise ClosureReceiptError("Closure receipt does not belong to the observed thread/root")
    return receipt


def observe_closure_receipt(
    github_client: GitHubClient,
    repo_name: str,
    pr_number: int,
    thread_id: str,
    root_comment_database_id: int,
    *,
    api_origin: str = "https://api.github.com",
) -> ReceiptObservation:
    """Obtain exact current thread, root, author, and receipt evidence."""
    if not _positive_id(root_comment_database_id):
        raise ClosureReceiptError("The requested closure root identity is invalid")
    try:
        authenticated_login = github_client.get_authenticated_user_login()
        threads = github_client.get_pr_review_threads_strict(repo_name, pr_number)
    except Exception as exc:
        raise ClosureReceiptError("Could not obtain authoritative closure receipt evidence") from exc
    if not isinstance(threads, (list, tuple)):
        raise ClosureReceiptError("GitHub did not return an authoritative thread collection")
    if any(not isinstance(thread, ReviewThread) for thread in threads):
        raise ClosureReceiptError("GitHub returned malformed review-thread data")
    matches = [thread for thread in threads if thread.id == thread_id]
    if len(matches) != 1:
        raise ClosureReceiptError("GitHub did not return the exact closure thread once")
    thread = matches[0]
    if not thread.comments or thread.comments_truncated or not isinstance(thread.is_resolved, bool) or not _positive_id(thread.comments[0].database_id) or thread.comments[0].database_id != root_comment_database_id:
        raise ClosureReceiptError("GitHub did not return a complete exact closure thread/root")
    receipt = parse_owned_receipt(thread, authenticated_login)
    if receipt is not None and (receipt.api_origin != normalize_api_origin(api_origin) or receipt.repository.casefold() != repo_name.casefold() or receipt.pr_number != pr_number):
        raise ClosureReceiptError("Owned closure receipt belongs to a different API/repository/PR namespace")
    return ReceiptObservation(thread=thread, authenticated_login=authenticated_login, receipt=receipt)


def _same_receipt(left: ClosureReceipt, right: ClosureReceipt) -> bool:
    return replace(left, comment_id=None) == replace(right, comment_id=None)


def _confirm_phase_state(observation: ReceiptObservation, desired: ClosureReceipt, *, confirmed_historical_rollback: bool = False) -> None:
    if desired.phase == "RESOLVED" and observation.thread.is_resolved is not True:
        raise ClosureReceiptError("GitHub has not confirmed the receipt's resolved outcome")
    if desired.phase == "ROLLED_BACK" and observation.thread.is_resolved is not False and not confirmed_historical_rollback:
        raise ClosureReceiptError("GitHub has not confirmed the receipt's rollback outcome")


def put_receipt(github_client: GitHubClient, observation: ReceiptObservation, desired: ClosureReceipt, *, confirmed_rollback_operation_key: str = "") -> ReceiptObservation:
    """Create or update the one owned reply and confirm the exact remote result.

    Callers hold ``closure_receipt_lock`` across the complete effect sequence.
    A fresh read here prevents using an absent-receipt observation captured
    before a contender completed. Response loss is resolved by read-back.

    A durable cleanup record may prove that this exact pending operation was
    already rolled back before a subsequent independent resolution. Its key
    permits retiring that old receipt without undoing the later resolution.
    It cannot rebind or otherwise rewrite the old operation's evidence.
    """
    _validate_receipt(desired)
    if observation.thread.id != desired.thread_id or not observation.thread.comments or observation.thread.comments[0].database_id != desired.root_comment_database_id:
        raise ClosureReceiptError("Closure receipt write does not match its observed thread/root")
    current = observe_closure_receipt(github_client, desired.repository, desired.pr_number, desired.thread_id, desired.root_comment_database_id, api_origin=desired.api_origin)
    if not is_same_github_login(current.authenticated_login, observation.authenticated_login):
        raise ClosureReceiptError("Controller identity changed before closure receipt publication")
    prior = current.receipt
    if observation.receipt is not None and (prior is None or prior.comment_id != observation.receipt.comment_id):
        raise ClosureReceiptError("Previously observed closure receipt disappeared or changed identity")
    if prior is None and desired.comment_id is not None:
        raise ClosureReceiptError("An observed closure receipt cannot be replaced by a new reply")
    if prior is not None and desired.comment_id is not None and desired.comment_id != prior.comment_id:
        raise ClosureReceiptError("Closure receipt write would replace a different owned reply")
    if prior is not None and prior.phase == "PENDING" and (prior.operation_key != desired.operation_key or prior.validated_head_sha != desired.validated_head_sha) and current.thread.is_resolved:
        raise ClosureReceiptError("A prior pending closure must be reconciled before rebinding its receipt")
    confirmed_historical_rollback = (
        isinstance(confirmed_rollback_operation_key, str)
        and bool(confirmed_rollback_operation_key)
        and prior is not None
        and prior.phase == "PENDING"
        and prior.operation_key == desired.operation_key == confirmed_rollback_operation_key
        and desired.phase == "ROLLED_BACK"
        and _same_receipt(replace(prior, phase="ROLLED_BACK"), desired)
    )
    _confirm_phase_state(current, desired, confirmed_historical_rollback=confirmed_historical_rollback)
    if prior is not None and _same_receipt(prior, desired):
        return current
    body = format_receipt(desired)
    write_error: Optional[Exception] = None
    try:
        if prior is None:
            github_client.reply_to_review_thread(desired.repository, desired.pr_number, desired.root_comment_database_id, body)
        else:
            if prior.comment_id is None:
                raise ClosureReceiptError("Observed closure receipt has no reply identity")
            github_client.update_review_thread_comment(desired.repository, prior.comment_id, body)
    except Exception as exc:
        write_error = exc
    try:
        confirmed = observe_closure_receipt(github_client, desired.repository, desired.pr_number, desired.thread_id, desired.root_comment_database_id, api_origin=desired.api_origin)
    except ClosureReceiptError as exc:
        raise ClosureReceiptError("Closure receipt publication could not be confirmed by a strict read") from (write_error or exc)
    if confirmed.receipt is None or not _same_receipt(confirmed.receipt, desired):
        raise ClosureReceiptError("GitHub did not confirm the exact closure receipt publication") from write_error
    if prior is not None and confirmed.receipt.comment_id != prior.comment_id:
        raise ClosureReceiptError("GitHub returned a replacement reply instead of the observed receipt")
    if not is_same_github_login(confirmed.authenticated_login, current.authenticated_login):
        raise ClosureReceiptError("Controller identity changed during closure receipt confirmation")
    # Exact body, reply ID and credential read-back above also preserve the
    # operation provenance on the historical-cleanup exception. A concurrently
    # rebound receipt can never inherit another operation's rollback proof.
    _confirm_phase_state(confirmed, desired, confirmed_historical_rollback=confirmed_historical_rollback)
    return confirmed


@contextmanager
def closure_receipt_lock(repo_name: str, pr_number: int, *, api_origin: str = "https://api.github.com", timeout: float = 0.0) -> Iterator[None]:
    """Fence publication and recovery in the same bounded runtime namespace."""
    identity = json.dumps([normalize_api_origin(api_origin), repo_name.casefold(), pr_number], separators=(",", ":"))
    path = lock_path(repo_name.casefold(), runtime_root() / "review-thread-closures", "closure-receipt", key=identity)
    ensure_lock_directory(path)
    with file_lock(path, timeout=timeout):
        yield
