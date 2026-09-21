"""Authenticated Dashboard operator write boundary for review adjudications.

This module owns Issue #2022: a narrow, opt-in server-side boundary that lets
a single authenticated Dashboard operator publish review adjudications under
a separately configured GitHub account. It never widens the existing
read-only Dashboard, and it never lets a browser-supplied assertion (a
hidden button, a client boolean, a copied cookie, or a raw request to one of
these routes) stand in for server-side authorization.

Every read of adjudication state reuses the authoritative reader built by
Issue #2018 (`AutomationEngine.get_review_adjudication_snapshots`); every
write reuses the pure v1 schema/renderer built by Issue #2017
(`review_adjudication.Decision` / `render_decision`). This module adds only
the operator session, the pre-send durable journal, and the strict identity
checks required to safely use a dedicated GitHub credential.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

from .automation_engine import AutomationEngine
from .llm_backend_config import (
    DashboardAdjudicationConfig,
    get_dashboard_adjudication_config,
    get_pr_review_allowlist_from_config,
    get_review_adjudicator_allowlist_from_config,
)
from .logger_config import get_logger
from .review_adjudication import Decision, render_decision
from .review_adjudication_orchestrator import (
    ADJUDICATION_EFFECTS_DB_ENV,
    DEFAULT_ADJUDICATION_EFFECTS_DB_PATH,
    AdjudicationEffectStore,
)
from .util.gh_cache import get_ghapi_client

logger = get_logger(__name__)

SESSION_COOKIE = "auto_coder_dashboard_adjudication_session"
SESSION_LIFETIME_SECONDS = 30 * 60
_ALLOWED_PAIRS = {("UPHOLD", "FIX"), ("OVERRULE", "NO_CHANGE"), ("UNDECIDED", "NONE")}


class AdjudicationAuthorizationError(HTTPException):
    """A server-boundary authorization rejection, always before any GitHub write."""


@dataclass
class _SessionRecord:
    secret_hash: str
    csrf_token: str
    expires_at: float


class DashboardAdjudicationSessionStore:
    """In-memory, process-local operator sessions.

    Deliberately not durable: REQ-002 requires a process restart to
    invalidate every session, which an in-memory store gives for free.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, _SessionRecord] = {}
        self._lock = threading.Lock()

    def create(self, secret_hash: str) -> tuple[str, str]:
        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[session_id] = _SessionRecord(secret_hash, csrf_token, time.time() + SESSION_LIFETIME_SECONDS)
        return session_id, csrf_token

    def get(self, session_id: str) -> Optional[_SessionRecord]:
        with self._lock:
            record = self._sessions.get(session_id)
            if record is None:
                return None
            if record.expires_at <= time.time():
                del self._sessions[session_id]
                return None
            return record

    def invalidate(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()


class AdjudicationPublicationJournal:
    """Durable pre-send record of every decision publication attempt.

    Guarantees required by REQ-007/REQ-008: a decision is never sent twice
    for the same accepted payload, a lost response never triggers a blind
    retry, and reusing a decision ID for a different payload is rejected.
    """

    _STATES = {"sending", "confirmed-published", "definitely-not-sent", "outcome-unknown"}

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS dashboard_adjudication_publications (
                decision_id TEXT PRIMARY KEY,
                repository TEXT NOT NULL,
                pr_number INTEGER NOT NULL,
                context_id TEXT NOT NULL,
                root_comment_id INTEGER NOT NULL,
                publisher_id INTEGER NOT NULL,
                payload_digest TEXT NOT NULL,
                payload_body TEXT NOT NULL,
                state TEXT NOT NULL,
                github_comment_id INTEGER
            )"""
        )
        self._db.commit()

    def claim(self, decision_id: str, repository: str, pr_number: int, context_id: str, root_comment_id: int, publisher_id: int, payload_digest: str, payload_body: str) -> tuple[str, Optional[int]]:
        """Atomically admit exactly one in-flight send per decision.

        Returns ``("claimed", None)`` only when this call must perform the
        GitHub POST. Every other outcome means a POST must not be issued.
        """
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT state, payload_digest, github_comment_id FROM dashboard_adjudication_publications WHERE decision_id=?", (decision_id,)).fetchone()
                if row is None:
                    self._db.execute(
                        "INSERT INTO dashboard_adjudication_publications(decision_id, repository, pr_number, context_id, root_comment_id, publisher_id, payload_digest, payload_body, state) VALUES(?,?,?,?,?,?,?,?, 'sending')",
                        (decision_id, repository, pr_number, context_id, root_comment_id, publisher_id, payload_digest, payload_body),
                    )
                    self._db.commit()
                    return "claimed", None
                state, existing_digest, comment_id = row
                if existing_digest != payload_digest:
                    self._db.commit()
                    raise ValueError(f"decision {decision_id} is already recorded for a different payload")
                if state == "confirmed-published":
                    self._db.commit()
                    return "already-published", (int(comment_id) if comment_id is not None else None)
                if state == "sending":
                    self._db.commit()
                    return "in-flight", None
                if state == "outcome-unknown":
                    self._db.commit()
                    return "needs-reconciliation", None
                # definitely-not-sent: safe to retry the send.
                self._db.execute("UPDATE dashboard_adjudication_publications SET state='sending' WHERE decision_id=?", (decision_id,))
                self._db.commit()
                return "claimed", None
            except BaseException:
                self._db.rollback()
                raise

    def mark_result(self, decision_id: str, to_state: str, github_comment_id: Optional[int] = None) -> None:
        if to_state not in self._STATES:
            raise ValueError(f"invalid publication state: {to_state}")
        with self._lock, self._db:
            self._db.execute("UPDATE dashboard_adjudication_publications SET state=?, github_comment_id=? WHERE decision_id=?", (to_state, github_comment_id, decision_id))

    def get(self, decision_id: str) -> Optional[tuple[str, int, int, int, str, str, Optional[int]]]:
        """Return (repository, pr_number, root_comment_id, publisher_id, payload_body, state, github_comment_id)."""
        row = self._db.execute(
            "SELECT repository, pr_number, root_comment_id, publisher_id, payload_body, state, github_comment_id FROM dashboard_adjudication_publications WHERE decision_id=?",
            (decision_id,),
        ).fetchone()
        if row is None:
            return None
        return str(row[0]), int(row[1]), int(row[2]), int(row[3]), str(row[4]), str(row[5]), (int(row[6]) if row[6] is not None else None)


class _LoginRequest(BaseModel):
    secret: str


class _DraftRequest(BaseModel):
    pr_number: int
    context_id: str
    verdict: str = "UNDECIDED"
    directive: str = "NONE"
    rationale: str = "Replace with the adjudicator rationale."


class _SubmitRequest(BaseModel):
    pr_number: int
    context_id: str
    decision_id: str
    head_sha: str
    contract_digest: str
    verdict: str
    directive: str
    rationale: str
    supersedes: tuple[str, ...] = ()


def _constant_time_secret_matches(candidate: str, secret_file: str) -> bool:
    try:
        expected = Path(os.path.expanduser(secret_file)).read_bytes().strip()
    except OSError:
        return False
    return hmac.compare_digest(expected, candidate.encode("utf-8"))


def _secret_hash(secret_file: str) -> Optional[str]:
    try:
        expected = Path(os.path.expanduser(secret_file)).read_bytes().strip()
    except OSError:
        return None
    return hashlib.sha256(expected).hexdigest()


def _resolve_publisher_identity(github_token: str) -> int:
    """Resolve the stable numeric GitHub ID proven by the dedicated credential."""
    api = get_ghapi_client(github_token)
    user = api.users.get_authenticated()
    user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        raise AdjudicationAuthorizationError(status_code=502, detail="GitHub did not return a stable publisher identity")
    return user_id


def _post_reply(github_token: str, repository: str, pr_number: int, root_comment_id: int, body: str) -> dict:
    owner, repo = repository.split("/")
    api = get_ghapi_client(github_token)
    response = api.pulls.create_reply_for_review_comment(owner, repo, pr_number, root_comment_id, body=body)
    return dict(response) if not isinstance(response, dict) else response


class AdjudicationWriteService:
    """FastAPI router implementing the authenticated write boundary."""

    def __init__(self, engine: AutomationEngine, repo_name: str) -> None:
        self.engine = engine
        self.repo_name = repo_name
        self.sessions = DashboardAdjudicationSessionStore()
        journal_path = Path(os.environ.get("AUTO_CODER_DASHBOARD_ADJUDICATION_JOURNAL_DB", "~/.auto-coder/dashboard-adjudication-journal.sqlite3")).expanduser()
        self.journal = AdjudicationPublicationJournal(journal_path)
        effects_path = Path(os.environ.get(ADJUDICATION_EFFECTS_DB_ENV, DEFAULT_ADJUDICATION_EFFECTS_DB_PATH)).expanduser()
        self.effects = AdjudicationEffectStore(effects_path)
        self.router = APIRouter()
        self._register_routes()

    def _config(self) -> DashboardAdjudicationConfig:
        config = get_dashboard_adjudication_config(repo_name=self.repo_name)
        if not config.enabled:
            # Disabling authoring (or a configuration defect) must not let a
            # session opened while it was enabled keep working.
            self.sessions.clear()
        return config

    def _require_origin(self, request: Request, config: DashboardAdjudicationConfig) -> None:
        origin = request.headers.get("origin")
        if not origin or origin != config.allowed_origin:
            raise AdjudicationAuthorizationError(status_code=403, detail="request origin is not the configured allowed_origin")

    def _require_session(self, request: Request, config: DashboardAdjudicationConfig) -> _SessionRecord:
        session_id = request.cookies.get(SESSION_COOKIE)
        if not session_id:
            raise AdjudicationAuthorizationError(status_code=401, detail="not authenticated")
        record = self.sessions.get(session_id)
        if record is None:
            raise AdjudicationAuthorizationError(status_code=401, detail="session is expired or unknown")
        current_hash = _secret_hash(config.operator_secret_file)
        if current_hash is None or current_hash != record.secret_hash:
            self.sessions.invalidate(session_id)
            raise AdjudicationAuthorizationError(status_code=401, detail="operator secret changed; session invalidated")
        return record

    def _require_csrf(self, request: Request, record: _SessionRecord) -> None:
        token = request.headers.get("x-csrf-token", "")
        if not token or not hmac.compare_digest(token, record.csrf_token):
            raise AdjudicationAuthorizationError(status_code=403, detail="missing or invalid CSRF token")

    def _authorize_read(self, request: Request) -> tuple[DashboardAdjudicationConfig, _SessionRecord]:
        config = self._config()
        if not config.enabled:
            raise AdjudicationAuthorizationError(status_code=403, detail="dashboard adjudication authoring is disabled")
        self._require_origin(request, config)
        record = self._require_session(request, config)
        return config, record

    def _authorize_mutation(self, request: Request) -> tuple[DashboardAdjudicationConfig, _SessionRecord]:
        config, record = self._authorize_read(request)
        self._require_csrf(request, record)
        return config, record

    def _snapshot_for_context(self, pr_number: int, context_id: str):
        snapshots = self.engine.get_review_adjudication_snapshots(self.repo_name, pr_number)
        for snapshot in snapshots:
            if snapshot.context is not None and snapshot.context.context_id == context_id:
                return snapshot
        return None

    def _register_routes(self) -> None:
        router = self.router

        @router.get("/availability")
        async def availability():
            """Expose setup state without exposing paths, credentials, or write authority."""
            config = self._config()
            return {
                "repository": self.repo_name,
                "configured": config.enabled,
                "diagnostic": config.diagnostic or ("ready for operator authentication" if config.enabled else "authoring is disabled"),
            }

        @router.post("/login")
        async def login(request: Request, response: Response, payload: _LoginRequest):
            config = self._config()
            if not config.enabled:
                raise AdjudicationAuthorizationError(status_code=403, detail="dashboard adjudication authoring is disabled")
            self._require_origin(request, config)
            if not _constant_time_secret_matches(payload.secret, config.operator_secret_file):
                logger.warning(f"repository={self.repo_name} dashboard_adjudication login rejected: invalid secret")
                raise AdjudicationAuthorizationError(status_code=401, detail="invalid operator secret")
            secret_hash = _secret_hash(config.operator_secret_file)
            if secret_hash is None:
                raise AdjudicationAuthorizationError(status_code=500, detail="operator secret became unreadable")
            session_id, csrf_token = self.sessions.create(secret_hash)
            response.set_cookie(
                key=SESSION_COOKIE,
                value=session_id,
                httponly=True,
                samesite="strict",
                secure=request.url.scheme == "https",
                max_age=SESSION_LIFETIME_SECONDS,
                path="/",
            )
            logger.info(f"repository={self.repo_name} dashboard_adjudication login accepted")
            return {"csrf_token": csrf_token, "expires_in": SESSION_LIFETIME_SECONDS}

        @router.post("/logout")
        async def logout(request: Request, response: Response):
            session_id = request.cookies.get(SESSION_COOKIE)
            if session_id:
                self.sessions.invalidate(session_id)
            response.delete_cookie(SESSION_COOKIE, path="/")
            return {"status": "logged-out"}

        @router.get("/session")
        async def session(request: Request):
            config, record = self._authorize_read(request)
            try:
                github_token = Path(os.path.expanduser(config.github_token_file)).read_text().strip()
                api = get_ghapi_client(github_token)
                user = api.users.get_authenticated()
                publisher_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)
                publisher_login = user.get("login") if isinstance(user, dict) else getattr(user, "login", None)
            except Exception as exc:
                logger.warning(f"repository={self.repo_name} dashboard_adjudication publisher identity unavailable: {type(exc).__name__}")
                raise HTTPException(status_code=502, detail="publishing account identity is unavailable") from exc
            try:
                allowlist = get_review_adjudicator_allowlist_from_config(repo_name=self.repo_name)
                valid_allowlist = isinstance(allowlist, list) and bool(allowlist) and all(isinstance(value, int) and not isinstance(value, bool) and value > 0 for value in allowlist)
            except ValueError:
                allowlist = None
                valid_allowlist = False
            authorized = valid_allowlist and isinstance(publisher_id, int) and publisher_id in (allowlist or [])
            return {
                "repository": self.repo_name,
                "publisher": {"id": publisher_id, "login": publisher_login},
                "configuration_valid": True,
                "authorization_valid": authorized,
                "authorization_reason": "authorized" if authorized else "publishing account is not in the effective repository adjudicator allowlist",
                "expires_at": record.expires_at,
            }

        @router.get("/context/{pr_number}")
        async def read_context(request: Request, pr_number: int):
            self._authorize_read(request)
            snapshots = self.engine.get_review_adjudication_snapshots(self.repo_name, pr_number)
            findings = []
            effects = {item.context_id: item for item in self.effects.rows_for_pr(self.repo_name, pr_number)}
            for snapshot in snapshots:
                if snapshot.context is None:
                    continue
                context = snapshot.context
                effect = effects.get(context.context_id)
                decisions = []
                for record in sorted(context.decisions.values(), key=lambda item: (item.source.created_at, item.source.comment_id)):
                    decisions.append(
                        {
                            "decision_id": record.decision.decision_id,
                            "verdict": record.decision.verdict,
                            "directive": record.decision.directive,
                            "rationale": record.decision.rationale,
                            "actor_id": record.source.author_id,
                            "comment_id": record.source.comment_id,
                            "created_at": record.source.created_at,
                            "supersedes": list(record.decision.supersedes),
                            "comment_url": f"https://github.com/{self.repo_name}/pull/{pr_number}#discussion_r{record.source.comment_id}",
                        }
                    )
                findings.append(
                    {
                        "context_id": context.context_id,
                        "root_comment_id": context.root_comment_id,
                        "head_sha": context.head_sha,
                        "base_sha": context.base_sha,
                        "base_ref": context.base_ref,
                        "contract_digest": context.contract_digest,
                        "contracts": [
                            {
                                "issue_number": item.issue_number,
                                "requirements": [{"id": requirement.id, "text": requirement.text} for requirement in item.requirements],
                            }
                            for item in context.contracts
                        ],
                        "contributing_issues": [item.issue_number for item in context.contracts],
                        "status": snapshot.result.status.value,
                        "reason": snapshot.result.reason,
                        "tips": list(snapshot.result.tips),
                        "retired_reason": context.retired_reason,
                        "raw_finding": snapshot.raw_finding,
                        "observation_revision": snapshot.observation_revision,
                        "actual_actor_id": snapshot.result.actual_actor_id,
                        "source_comment_id": snapshot.result.source_comment_id,
                        "active_decision_id": snapshot.result.decision_id,
                        "active_verdict": snapshot.result.verdict,
                        "active_directive": snapshot.result.directive,
                        "decisions": decisions,
                        "processing": ({"decision_id": effect.decision_id, "verdict": effect.verdict, "status": effect.status} if effect is not None else None),
                    }
                )
            return {"pr_number": pr_number, "findings": findings}

        @router.post("/draft")
        async def draft(request: Request, payload: _DraftRequest):
            self._authorize_read(request)
            if (payload.verdict, payload.directive) not in _ALLOWED_PAIRS:
                raise HTTPException(status_code=400, detail="unsupported verdict/directive pair")
            if not payload.rationale.strip():
                raise HTTPException(status_code=400, detail="rationale must be nonblank")
            snapshot = self._snapshot_for_context(payload.pr_number, payload.context_id)
            if snapshot is None or snapshot.context is None:
                raise HTTPException(status_code=404, detail="no applicable review context found")
            context = snapshot.context
            tips = list(snapshot.result.tips)
            decision_id = str(uuid.uuid4())
            example = Decision(
                decision_id=decision_id,
                context_id=context.context_id,
                head_sha=context.head_sha,
                contract_digest=context.contract_digest,
                verdict=payload.verdict,
                directive=payload.directive,
                supersedes=tuple(tips),
                rationale=payload.rationale,
                source="dashboard",
            )
            return {
                "decision_id": decision_id,
                "pr_number": payload.pr_number,
                "context_id": context.context_id,
                "root_comment_id": context.root_comment_id,
                "head_sha": context.head_sha,
                "contract_digest": context.contract_digest,
                "tips": tips,
                "allowed_pairs": [{"verdict": v, "directive": d} for v, d in sorted(_ALLOWED_PAIRS)],
                "proposed_body": render_decision(example),
            }

        @router.post("/submit")
        async def submit(request: Request, payload: _SubmitRequest):
            config, _record = self._authorize_mutation(request)

            pair = (payload.verdict, payload.directive)
            if pair not in _ALLOWED_PAIRS:
                raise HTTPException(status_code=400, detail="unsupported verdict/directive pair")
            if not payload.rationale.strip():
                raise HTTPException(status_code=400, detail="rationale must be nonblank")

            candidate_body = render_decision(
                Decision(
                    decision_id=payload.decision_id,
                    context_id=payload.context_id,
                    head_sha=payload.head_sha,
                    contract_digest=payload.contract_digest,
                    verdict=payload.verdict,
                    directive=payload.directive,
                    supersedes=tuple(sorted(set(payload.supersedes))),
                    rationale=payload.rationale,
                    source="dashboard",
                )
            )
            existing = self.journal.get(payload.decision_id)
            if existing is not None:
                _repo, _pr, _root, _publisher, existing_body, existing_state, existing_comment_id = existing
                if existing_body != candidate_body:
                    raise HTTPException(status_code=409, detail=f"decision {payload.decision_id} is already recorded for a different payload")
                # A resend of an already-resolved decision is answered from the
                # durable journal alone: publication (or its confirmed refusal)
                # must never be re-derived from a snapshot that a later,
                # unrelated event (e.g. this same publish's own reconciliation
                # trigger) may since have marked unavailable.
                if existing_state == "confirmed-published":
                    return {"status": "published-awaiting-processing", "decision_id": payload.decision_id, "github_comment_id": existing_comment_id}
                if existing_state == "sending":
                    return {"status": "outcome-unknown", "decision_id": payload.decision_id, "github_comment_id": None}
                if existing_state == "outcome-unknown":
                    state, comment_id = self._reconcile(payload.decision_id)
                    return {"status": state, "decision_id": payload.decision_id, "github_comment_id": comment_id}
                # existing_state == "definitely-not-sent": a genuine retry,
                # which still needs the full re-authorization below.

            try:
                github_token = Path(os.path.expanduser(config.github_token_file)).read_bytes().decode("utf-8").strip()
            except OSError as exc:
                logger.error(f"repository={self.repo_name} dashboard_adjudication github_token_file unreadable at submit: {type(exc).__name__}")
                raise HTTPException(status_code=500, detail="publishing credential is unavailable") from exc

            try:
                publisher_id = _resolve_publisher_identity(github_token)
            except httpx.HTTPError as exc:
                raise HTTPException(status_code=502, detail="failed to resolve the publishing account's identity") from exc

            adjudicator_ids = get_review_adjudicator_allowlist_from_config(repo_name=self.repo_name) or []
            if publisher_id not in adjudicator_ids:
                logger.warning(f"repository={self.repo_name} pr={payload.pr_number} dashboard_adjudication rejected: publisher not in review_adjudicator_allowlist")
                raise HTTPException(status_code=403, detail="the configured publishing account is not an authorized adjudicator")

            snapshot = self._snapshot_for_context(payload.pr_number, payload.context_id)
            if snapshot is None or snapshot.context is None:
                raise HTTPException(status_code=409, detail="review context is no longer available; refresh and retry")
            context = snapshot.context
            if context.retired_reason is not None:
                raise HTTPException(status_code=409, detail=f"review context was retired: {context.retired_reason}")
            if snapshot.result.status.value == "SOURCE_UNAVAILABLE":
                raise HTTPException(status_code=409, detail="authoritative evidence for this context is currently unavailable")
            if context.root_actor_type != "Bot":
                raise HTTPException(status_code=403, detail="target root is not an automated reviewer thread")
            reviewer_ids = get_pr_review_allowlist_from_config(repo_name=self.repo_name) or []
            if context.root_author_id not in reviewer_ids:
                raise HTTPException(status_code=403, detail="target root author is not in the configured pr_review_allowlist")
            if context.head_sha != payload.head_sha or context.contract_digest != payload.contract_digest:
                raise HTTPException(status_code=409, detail="head or contract digest changed since the draft was prepared; refresh and retry")
            current_tips = set(snapshot.result.tips)
            if set(payload.supersedes) != current_tips:
                raise HTTPException(status_code=409, detail={"message": "supersedes must name every current predecessor tip", "current_tips": sorted(current_tips)})

            pr_data = self.engine.github.get_pull_request_metadata_strict(self.repo_name, payload.pr_number)
            if not isinstance(pr_data, dict) or pr_data.get("state") != "open":
                raise HTTPException(status_code=409, detail="target pull request is not open")

            # payload.head_sha/contract_digest/supersedes were just verified to
            # equal the fresh authoritative context above, so the candidate
            # body computed from client-submitted fields is exactly what an
            # independent reconstruction from the context would produce.
            body = candidate_body
            payload_digest = hashlib.sha256(body.encode("utf-8")).hexdigest()

            try:
                outcome, existing_comment_id = self.journal.claim(payload.decision_id, self.repo_name, payload.pr_number, context.context_id, context.root_comment_id, publisher_id, payload_digest, body)
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

            if outcome == "already-published":
                return {"status": "published-awaiting-processing", "decision_id": payload.decision_id, "github_comment_id": existing_comment_id}
            if outcome == "in-flight":
                return {"status": "outcome-unknown", "decision_id": payload.decision_id, "github_comment_id": None}
            if outcome == "needs-reconciliation":
                result = self._reconcile(payload.decision_id)
                return {"status": result[0], "decision_id": payload.decision_id, "github_comment_id": result[1]}

            # outcome == "claimed": this request owns the one permitted send.
            try:
                response = _post_reply(github_token, self.repo_name, payload.pr_number, context.root_comment_id, body)
            except httpx.HTTPStatusError as exc:
                if 400 <= exc.response.status_code < 500 and exc.response.status_code not in (408, 429):
                    self.journal.mark_result(payload.decision_id, "definitely-not-sent")
                    logger.error(f"repository={self.repo_name} pr={payload.pr_number} context={context.context_id} decision={payload.decision_id} publication refused: {exc.response.status_code}")
                    raise HTTPException(status_code=502, detail="GitHub refused the publication") from exc
                self.journal.mark_result(payload.decision_id, "outcome-unknown")
                logger.warning(f"repository={self.repo_name} pr={payload.pr_number} context={context.context_id} decision={payload.decision_id} publication outcome unknown after HTTP error")
                return {"status": "outcome-unknown", "decision_id": payload.decision_id, "github_comment_id": None}
            except Exception:
                self.journal.mark_result(payload.decision_id, "outcome-unknown")
                logger.warning(f"repository={self.repo_name} pr={payload.pr_number} context={context.context_id} decision={payload.decision_id} publication outcome unknown after transport failure")
                return {"status": "outcome-unknown", "decision_id": payload.decision_id, "github_comment_id": None}

            comment_id = response.get("id")
            comment_body = response.get("body")
            comment_author = response.get("user", {}).get("id") if isinstance(response.get("user"), dict) else None
            if not isinstance(comment_id, int) or comment_body != body or comment_author != publisher_id:
                # GitHub's own response does not confirm target/body/author:
                # never report success from an unconfirmed write.
                self.journal.mark_result(payload.decision_id, "outcome-unknown")
                logger.warning(f"repository={self.repo_name} pr={payload.pr_number} context={context.context_id} decision={payload.decision_id} publication response did not confirm target/body/author")
                return {"status": "outcome-unknown", "decision_id": payload.decision_id, "github_comment_id": None}

            self.journal.mark_result(payload.decision_id, "confirmed-published", comment_id)
            logger.info(f"repository={self.repo_name} pr={payload.pr_number} context={context.context_id} decision={payload.decision_id} publication confirmed comment={comment_id}")
            await self.engine.invalidate_entity(self.repo_name, "pr", payload.pr_number, event_type="dashboard_adjudication", action="published")
            return {"status": "published-awaiting-processing", "decision_id": payload.decision_id, "github_comment_id": comment_id}

        @router.get("/status/{decision_id}")
        async def status(request: Request, decision_id: str):
            self._authorize_read(request)
            record = self.journal.get(decision_id)
            if record is None:
                raise HTTPException(status_code=404, detail="unknown decision id")
            repository, pr_number, _root_comment_id, _publisher_id, _body, state, comment_id = record
            if repository != self.repo_name:
                raise HTTPException(status_code=404, detail="unknown decision id")
            if state == "outcome-unknown":
                state, comment_id = self._reconcile(decision_id)
            return {"decision_id": decision_id, "pr_number": pr_number, "state": state, "github_comment_id": comment_id}

    def _reconcile(self, decision_id: str) -> tuple[str, Optional[int]]:
        """Resolve an unknown outcome from the exact authoritative thread, never by POSTing again."""
        record = self.journal.get(decision_id)
        if record is None:
            return "error", None
        repository, pr_number, root_comment_id, publisher_id, body, state, comment_id = record
        if state != "outcome-unknown":
            return state, comment_id
        try:
            threads = self.engine.github.get_pr_review_threads_strict(repository, pr_number)
        except Exception:
            logger.warning(f"repository={repository} pr={pr_number} decision={decision_id} reconciliation read failed; outcome remains unknown")
            return "outcome-unknown", None
        thread = next((item for item in threads if item.comments and item.comments[0].database_id == root_comment_id), None)
        if thread is None or thread.comments_truncated:
            return "outcome-unknown", None
        match = next((comment for comment in thread.comments if comment.body == body and comment.author_id == publisher_id), None)
        if match is not None and match.database_id is not None:
            self.journal.mark_result(decision_id, "confirmed-published", match.database_id)
            logger.info(f"repository={repository} pr={pr_number} decision={decision_id} reconciliation confirmed comment={match.database_id}")
            return "confirmed-published", match.database_id
        # A complete authoritative thread read without the expected reply
        # proves the earlier attempt was never accepted.
        self.journal.mark_result(decision_id, "definitely-not-sent")
        logger.info(f"repository={repository} pr={pr_number} decision={decision_id} reconciliation found no matching reply; safe to retry")
        return "definitely-not-sent", None


def init_dashboard_adjudication(app: FastAPI, engine: AutomationEngine, repo_name: str) -> AdjudicationWriteService:
    """Mount the authenticated adjudication write boundary alongside the read-only Dashboard.

    Mounting is unconditional; every route independently checks
    ``[dashboard_adjudication].enabled`` on each request so a configuration
    change takes effect without a remount, and disabled/misconfigured
    deployments reject every route with 403 rather than leaking whether the
    feature exists via 404.
    """
    service = AdjudicationWriteService(engine, repo_name)
    app.include_router(service.router, prefix="/dashboard-adjudication")
    return service
