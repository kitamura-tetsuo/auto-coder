"""Durable, evidence-based Codex Cloud pull-request attribution."""

from __future__ import annotations

import fcntl
import json
import os
import re
import threading
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from .cloud_run import CloudRun, CloudRunRepository
from .runtime_locks import ensure_lock_directory, lock_path


class AttributionDisposition(str, Enum):
    VERIFIED = "VERIFIED"
    UNRESOLVED = "UNRESOLVED"
    UNAVAILABLE = "UNAVAILABLE"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True)
class CodexPrOrigin:
    repository: str
    pr_number: int
    issue_number: int
    provider: str
    task_id: str
    backend_name: str
    attempt: int
    launch_identity: str
    evidence: str
    revision: int


@dataclass(frozen=True)
class AttributionResult:
    disposition: AttributionDisposition
    origin: Optional[CodexPrOrigin] = None
    boundary: str = ""
    consistency_token: str = ""


_TASK_PATH = re.compile(r"^/codex(?:/cloud)?/tasks/(task_e_[A-Za-z0-9]+)/?$")
_URL_CANDIDATE = re.compile(r"https://[^\s<>()]+", re.IGNORECASE)


def task_ids_from_text(text: object) -> set[str]:
    """Extract only provider-supported, structurally exact task URLs."""
    if not isinstance(text, str):
        return set()
    result: set[str] = set()
    for raw in _URL_CANDIDATE.findall(text):
        try:
            parsed = urlparse(raw.rstrip(".,;"))
            if parsed.scheme != "https" or parsed.hostname not in {"chatgpt.com", "chat.openai.com"}:
                continue
            if parsed.username or parsed.password or parsed.port is not None:
                continue
            match = _TASK_PATH.fullmatch(parsed.path)
            if match:
                result.add(match.group(1))
        except ValueError:
            continue
    return result


def closes_issue(pr: dict[str, object], repository: str, issue_number: int) -> bool:
    """Return whether authoritative PR metadata explicitly closes the Issue."""
    relations = pr.get("closingIssuesReferences")
    if isinstance(relations, list):
        for relation in relations:
            if isinstance(relation, dict) and relation.get("number") == issue_number:
                owner, _, name = repository.partition("/")
                relation_repo = relation.get("repository")
                relation_owner = relation_repo.get("owner") if isinstance(relation_repo, dict) else None
                if not isinstance(relation_repo, dict) or (relation_repo.get("name") == name and isinstance(relation_owner, dict) and relation_owner.get("login") == owner):
                    return True
    raw_body = pr.get("body")
    body: str = raw_body if isinstance(raw_body, str) else ""
    verb = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
    local = rf"(?i)\b{verb}\s+#%d(?!\d)" % issue_number
    full = rf"(?i)\b{verb}\s+https://github\.com/{re.escape(repository)}/issues/{issue_number}(?!\d)"
    return bool(re.search(local, body) or re.search(full, body))


class CodexPrAttributionRepository:
    """Atomic repository-wide PR binding registry with conflict detection."""

    def __init__(self, repository: str, storage_path: Optional[Path] = None):
        self.repository = repository
        self.path = storage_path or Path.home() / ".auto-coder" / repository / "codex_pr_attributions.json"
        self.lock_path = lock_path(repository, self.path, "codex-pr-attribution")
        self._lock = threading.Lock()

    def _read(self) -> dict[str, object]:
        if not self.path.exists():
            return {"revision": 0, "bindings": {}}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("bindings"), dict) or not isinstance(value.get("revision"), int):
            raise ValueError("invalid Codex PR attribution registry")
        return value

    def get(self, pr_number: int) -> AttributionResult:
        try:
            value = self._read()
            bindings = value["bindings"]
            if not isinstance(bindings, dict):
                raise ValueError("invalid bindings")
            raw = bindings.get(str(pr_number))
            token = str(value["revision"])
            if raw is None:
                return AttributionResult(AttributionDisposition.UNRESOLVED, boundary="no verified PR binding", consistency_token=token)
            if not isinstance(raw, dict):
                raise ValueError("invalid binding")
            return AttributionResult(AttributionDisposition.VERIFIED, CodexPrOrigin(**raw), consistency_token=token)
        except OSError as exc:
            return AttributionResult(AttributionDisposition.UNAVAILABLE, boundary=f"attribution read failed: {type(exc).__name__}")
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            return AttributionResult(AttributionDisposition.CONFLICT, boundary=f"attribution record is corrupt: {type(exc).__name__}")

    def establish(self, candidate: CodexPrOrigin) -> AttributionResult:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                ensure_lock_directory(self.lock_path)
                with open(self.lock_path, "a+", encoding="utf-8") as locked:
                    fcntl.flock(locked.fileno(), fcntl.LOCK_EX)
                    value = self._read()
                    bindings = value["bindings"]
                    assert isinstance(bindings, dict)
                    raw = bindings.get(str(candidate.pr_number))
                    if raw is not None:
                        if not isinstance(raw, dict):
                            raise ValueError("invalid binding")
                        current = CodexPrOrigin(**raw)
                        current_identity = (current.repository, current.pr_number, current.issue_number, current.provider, current.task_id, current.backend_name, current.attempt, current.launch_identity)
                        candidate_identity = (candidate.repository, candidate.pr_number, candidate.issue_number, candidate.provider, candidate.task_id, candidate.backend_name, candidate.attempt, candidate.launch_identity)
                        if current_identity != candidate_identity:
                            return AttributionResult(AttributionDisposition.CONFLICT, current, "PR already has an incompatible verified origin", str(value["revision"]))
                        return AttributionResult(AttributionDisposition.VERIFIED, current, consistency_token=str(value["revision"]))
                    stored_revision = value["revision"]
                    if not isinstance(stored_revision, int):
                        raise ValueError("invalid revision")
                    revision = stored_revision + 1
                    committed = CodexPrOrigin(**{**asdict(candidate), "revision": revision})
                    bindings[str(candidate.pr_number)] = asdict(committed)
                    value["revision"] = revision
                    temporary = self.path.with_suffix(f".json.{os.getpid()}.{threading.get_ident()}.tmp")
                    with open(temporary, "x", encoding="utf-8") as stream:
                        json.dump(value, stream, indent=2, sort_keys=True)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, self.path)
                    directory_fd = os.open(str(self.path.parent), os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                    return AttributionResult(AttributionDisposition.VERIFIED, committed, consistency_token=str(revision))
        except OSError as exc:
            return AttributionResult(AttributionDisposition.UNAVAILABLE, boundary=f"attribution write failed: {type(exc).__name__}")
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            return AttributionResult(AttributionDisposition.CONFLICT, boundary=f"attribution record is corrupt: {type(exc).__name__}")


def resolve_codex_pr_origin(repository: str, pr: dict[str, object], runs: CloudRunRepository, bindings: CodexPrAttributionRepository) -> AttributionResult:
    """Resolve and persist a PR origin from accepted launch/publication proof."""
    number = pr.get("number")
    if not isinstance(number, int):
        return AttributionResult(AttributionDisposition.UNAVAILABLE, boundary="authoritative PR number is unavailable")
    existing = bindings.get(number)
    if existing.disposition in {AttributionDisposition.UNAVAILABLE, AttributionDisposition.CONFLICT}:
        return existing
    try:
        accepted = [run for run in runs.list_all() if run.provider == "codex-cloud" and run.submission_outcome == "accepted" and run.task_id]
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        return AttributionResult(AttributionDisposition.UNAVAILABLE, boundary=f"accepted launch read failed: {type(exc).__name__}")
    url_ids = task_ids_from_text(pr.get("body"))
    candidates: list[tuple[CloudRun, str]] = []
    head = pr.get("head")
    head_ref = head.get("ref") if isinstance(head, dict) else pr.get("headRefName")
    raw_head_repo = head.get("repo") if isinstance(head, dict) else None
    graph_head_repo = pr.get("headRepository")
    if isinstance(raw_head_repo, dict):
        head_repo = raw_head_repo.get("full_name")
    elif isinstance(graph_head_repo, dict):
        head_repo = graph_head_repo.get("nameWithOwner")
    else:
        head_repo = ""
    for run in accepted:
        if not closes_issue(pr, repository, run.issue_number):
            continue
        if run.task_id in url_ids:
            candidates.append((run, "authoritative-pr-task-url+closing-reference"))
        elif run.publication_head_repository == head_repo and run.publication_head_ref == head_ref and head_repo == repository:
            candidates.append((run, "retained-publication-intent+closing-reference"))
    unique = {(run.task_id, run.issue_number, run.backend_name, run.attempt, run.launch_identity): (run, evidence) for run, evidence in candidates}
    if len(url_ids) > 1 or len(unique) > 1:
        return AttributionResult(AttributionDisposition.CONFLICT, boundary="incompatible qualifying accepted-task origins")
    if not unique:
        return existing if existing.disposition is AttributionDisposition.VERIFIED else AttributionResult(AttributionDisposition.UNRESOLVED, boundary="no coherent accepted task and qualifying PR publication proof")
    run, evidence = next(iter(unique.values()))
    origin = CodexPrOrigin(repository, number, run.issue_number, run.provider, run.task_id, run.backend_name, run.attempt, run.launch_identity, evidence, 0)
    if existing.disposition is AttributionDisposition.VERIFIED and existing.origin is not None:
        established = existing.origin
        candidate_identity = (origin.issue_number, origin.provider, origin.task_id, origin.backend_name, origin.attempt, origin.launch_identity)
        established_identity = (established.issue_number, established.provider, established.task_id, established.backend_name, established.attempt, established.launch_identity)
        if candidate_identity != established_identity:
            return AttributionResult(AttributionDisposition.CONFLICT, established, "fresh qualifying proof conflicts with the verified PR origin", existing.consistency_token)
        return existing
    return bindings.establish(origin)
