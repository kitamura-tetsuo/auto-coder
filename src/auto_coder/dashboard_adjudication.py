import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from .automation_engine import AutomationEngine
from .llm_backend_config import get_review_adjudicator_allowlist_from_config
from .review_adjudication import Decision, render_decision
from .util.gh_cache import get_ghapi_client


class AdjudicationPublicationJournal:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS publications (
                decision_id TEXT PRIMARY KEY,
                repository TEXT NOT NULL,
                pr_number INTEGER NOT NULL,
                publisher_id INTEGER NOT NULL,
                payload_digest TEXT NOT NULL,
                state TEXT NOT NULL,
                github_comment_id INTEGER
            )
            """
        )
        self._db.commit()

    def record_attempt(self, decision_id: str, repository: str, pr_number: int, publisher_id: int, payload_digest: str) -> str:
        with self._lock:
            cur = self._db.cursor()
            cur.execute("SELECT state, payload_digest FROM publications WHERE decision_id = ?", (decision_id,))
            row = cur.fetchone()
            if row:
                state, existing_digest = row
                if existing_digest != payload_digest:
                    raise ValueError(f"Decision ID {decision_id} is already in use for a different payload")
                return state

            cur.execute("INSERT INTO publications (decision_id, repository, pr_number, publisher_id, payload_digest, state) VALUES (?, ?, ?, ?, ?, ?)", (decision_id, repository, pr_number, publisher_id, payload_digest, "prepared"))
            self._db.commit()
            return "prepared"

    def transition_state(self, decision_id: str, from_state: str, to_state: str, github_comment_id: Optional[int] = None) -> bool:
        with self._lock:
            cur = self._db.cursor()
            cur.execute("SELECT state FROM publications WHERE decision_id = ?", (decision_id,))
            row = cur.fetchone()
            if not row or row[0] != from_state:
                return False

            if github_comment_id is not None:
                cur.execute("UPDATE publications SET state = ?, github_comment_id = ? WHERE decision_id = ?", (to_state, github_comment_id, decision_id))
            else:
                cur.execute("UPDATE publications SET state = ? WHERE decision_id = ?", (to_state, decision_id))
            self._db.commit()
            return True

    def get_state(self, decision_id: str) -> Optional[tuple[str, Optional[int]]]:
        with self._lock:
            cur = self._db.cursor()
            cur.execute("SELECT state, github_comment_id FROM publications WHERE decision_id = ?", (decision_id,))
            return cur.fetchone()


class SessionData:
    def __init__(self, repository: str, expires_at: float, secret_hash: str):
        self.repository = repository
        self.expires_at = expires_at
        self.secret_hash = secret_hash
        self.csrf_token = secrets.token_hex(32)


class SessionManager:
    def __init__(self):
        self._sessions: Dict[str, SessionData] = {}
        self._lock = threading.Lock()

    def create_session(self, repository: str, secret_hash: str) -> tuple[str, str]:
        with self._lock:
            session_id = secrets.token_urlsafe(32)
            self._sessions[session_id] = SessionData(repository=repository, expires_at=time.time() + 1800, secret_hash=secret_hash)  # 30 minutes
            return session_id, self._sessions[session_id].csrf_token

    def get_session(self, session_id: str) -> Optional[SessionData]:
        with self._lock:
            if session_id in self._sessions:
                session = self._sessions[session_id]
                if time.time() > session.expires_at:
                    del self._sessions[session_id]
                    return None
                return session
            return None

    def invalidate_session(self, session_id: str) -> None:
        with self._lock:
            if session_id in self._sessions:
                del self._sessions[session_id]

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()


class LoginRequest(BaseModel):
    secret: str


class PrepareRequest(BaseModel):
    pr_number: int


class SubmitRequest(BaseModel):
    pr_number: int
    decision_id: str
    verdict: str
    directive: str
    rationale: str
    context_id: str
    head_sha: str
    supersedes: Tuple[str, ...]


class StatusResponse(BaseModel):
    decision_id: str
    state: str
    github_comment_id: Optional[int]


def _validate_github_identity(token: str, allowlist: Optional[List[int]]) -> int:
    try:
        response = httpx.get("https://api.github.com/user", headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}, timeout=10.0)
        response.raise_for_status()
        user_id = response.json().get("id")
        if not user_id:
            raise ValueError("No user ID in GitHub response")

        if allowlist is None or user_id not in allowlist:
            raise ValueError(f"GitHub user ID {user_id} is not in review_adjudicator_allowlist")
        return user_id
    except httpx.HTTPError as e:
        raise ValueError(f"Failed to fetch GitHub user ID: {e}")


def require_auth(req: Request, service: "AdjudicationService") -> tuple[SessionData, Any]:
    repo_name = req.query_params.get("repository")
    if not repo_name:
        raise HTTPException(status_code=400, detail="repository query param required")

    config = service.config_factory(repo_name)
    if not config.enabled:
        raise HTTPException(status_code=403, detail="Adjudication authoring is disabled")

    origin = req.headers.get("origin")
    if not origin or origin != config.allowed_origin:
        raise HTTPException(status_code=403, detail="Invalid origin")

    session_id = req.cookies.get("adjudication_session")
    if not session_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    session = service.session_manager.get_session(session_id)
    if not session or session.repository != repo_name:
        raise HTTPException(status_code=401, detail="Invalid session")

    try:
        with open(os.path.expanduser(config.operator_secret_file), "r", encoding="utf-8") as f:
            expected_secret = f.read().strip()
            expected_hash = hashlib.sha256(expected_secret.encode("utf-8")).hexdigest()
            if getattr(session, "secret_hash", None) != expected_hash:
                service.session_manager.invalidate_session(session_id)
                raise HTTPException(status_code=401, detail="Invalid session (secret changed)")
    except OSError:
        raise HTTPException(status_code=500, detail="Cannot read operator secret")

    return session, config


def require_csrf(req: Request, session: SessionData) -> None:
    token = req.headers.get("x-csrf-token")
    if not token or not hmac.compare_digest(token.encode("utf-8"), session.csrf_token.encode("utf-8")):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


class AdjudicationService:
    def __init__(self, config_factory: Callable[[str], Any], engine: AutomationEngine):
        self.config_factory = config_factory
        self.engine = engine
        self.session_manager = SessionManager()
        db_path = Path(os.environ.get("AUTO_CODER_REVIEW_ADJUDICATION_JOURNAL_DB", "~/.auto-coder/review-adjudication-journal.sqlite3")).expanduser()
        self.journal = AdjudicationPublicationJournal(db_path)
        self.router = APIRouter()
        self._setup_routes()

    def _setup_routes(self):
        @self.router.post("/login")
        async def login(req: Request, response: Response, payload: LoginRequest):
            repo_name = req.query_params.get("repository")
            if not repo_name:
                raise HTTPException(status_code=400, detail="repository query param required")

            config = self.config_factory(repo_name)
            if not config.enabled:
                raise HTTPException(status_code=403, detail="Adjudication authoring is disabled")

            origin = req.headers.get("origin")
            if not origin or origin != config.allowed_origin:
                raise HTTPException(status_code=403, detail="Invalid origin")

            try:
                with open(os.path.expanduser(config.operator_secret_file), "r", encoding="utf-8") as f:
                    expected_secret = f.read().strip()
            except OSError:
                raise HTTPException(status_code=500, detail="Cannot read operator secret")

            expected_bytes = expected_secret.encode("utf-8")
            provided_bytes = payload.secret.encode("utf-8")
            if len(expected_bytes) != len(provided_bytes) or not hmac.compare_digest(expected_bytes, provided_bytes):
                raise HTTPException(status_code=401, detail="Invalid secret")

            secret_hash = hashlib.sha256(expected_bytes).hexdigest()
            session_id, csrf_token = self.session_manager.create_session(repo_name, secret_hash)

            is_https = req.url.scheme == "https"
            response.set_cookie(key="adjudication_session", value=session_id, httponly=True, samesite="strict", secure=is_https, max_age=1800)
            return {"csrf_token": csrf_token}

        @self.router.post("/logout")
        async def logout(req: Request, response: Response):
            session_id = req.cookies.get("adjudication_session")
            if session_id:
                self.session_manager.invalidate_session(session_id)
            response.delete_cookie("adjudication_session")
            return {"status": "ok"}

        @self.router.post("/prepare")
        async def prepare(req: Request, payload: PrepareRequest):
            session, config = require_auth(req, self)
            require_csrf(req, session)

            repo_name = session.repository
            pr_number = payload.pr_number

            try:
                snapshots = self.engine.get_review_adjudication_snapshots(repo_name, pr_number)
                if not snapshots:
                    raise HTTPException(status_code=404, detail="No applicable review context found")

                snapshot = snapshots[0]
                if not snapshot.context:
                    raise HTTPException(status_code=400, detail="Missing complete context for adjudication")
            except Exception as e:
                raise HTTPException(status_code=400, detail=str(e))

            decision_id = str(uuid.uuid4())

            return {
                "decision_id": decision_id,
                "pr_number": pr_number,
                "context_id": snapshot.context.context_id,
                "head_sha": snapshot.context.head_sha,
                "contract_digest": snapshot.context.integrity_digest,
                "verdicts": ["UPHOLD", "OVERRULE", "UNDECIDED"],
                "directives": ["FIX", "NO_CHANGE", "NONE"],
                "supersedes": snapshot.result.tips if snapshot.result else tuple(),
                "proposed_body": render_decision(
                    Decision(
                        decision_id=decision_id,
                        context_id=snapshot.context.context_id,
                        head_sha=snapshot.context.head_sha,
                        contract_digest=snapshot.context.integrity_digest,
                        verdict="UPHOLD",
                        directive="FIX",
                        supersedes=snapshot.result.tips if snapshot.result else tuple(),
                        rationale="Your rationale here",
                        source="dashboard",
                    )
                ),
            }

        @self.router.post("/submit")
        async def submit(req: Request, payload: SubmitRequest):
            session, config = require_auth(req, self)
            require_csrf(req, session)

            repo_name = session.repository
            pr_number = payload.pr_number

            # Fetch GitHub ID for auth'd user
            try:
                with open(os.path.expanduser(config.github_token_file), "r", encoding="utf-8") as f:
                    token = f.read().strip()
            except OSError:
                raise HTTPException(status_code=500, detail="Cannot read GitHub token")

            allowlist = get_review_adjudicator_allowlist_from_config(repo_name=repo_name)
            try:
                publisher_id = _validate_github_identity(token, allowlist)
            except ValueError as e:
                raise HTTPException(status_code=403, detail=str(e))

            # Re-read context and ensure valid
            try:
                snapshots = self.engine.get_review_adjudication_snapshots(repo_name, pr_number)
                if not snapshots:
                    raise HTTPException(status_code=400, detail="No applicable review context found during submission")
                snapshot = snapshots[0]
                if not snapshot.context:
                    raise HTTPException(status_code=400, detail="Missing complete context")
            except Exception as e:
                raise HTTPException(status_code=400, detail=str(e))

            # Validate target is an open PR (and github root author check)
            try:
                gh_client = get_ghapi_client(self.engine.github.token)
                pr = gh_client.pulls.get(owner=repo_name.split("/")[0], repo=repo_name.split("/")[1], pull_number=pr_number)
                if pr.state != "open":
                    raise HTTPException(status_code=400, detail="Target PR is not open")
            except Exception as e:
                if isinstance(e, HTTPException):
                    raise
                raise HTTPException(status_code=400, detail=f"Failed to check PR state: {e}")

            from .llm_backend_config import get_pr_review_allowlist_from_config

            pr_review_allowlist = get_pr_review_allowlist_from_config(repo_name=repo_name)
            if pr_review_allowlist is not None and snapshot.context.root_author_id not in pr_review_allowlist:
                raise HTTPException(status_code=400, detail="Root comment author is not in pr_review_allowlist")

            # Validate snapshot hasn't changed (REQ-005 conflict check)
            current_tips = tuple(snapshot.result.tips) if snapshot.result else tuple()
            if payload.context_id != snapshot.context.context_id or payload.head_sha != snapshot.context.head_sha or set(payload.supersedes) != set(current_tips):
                raise HTTPException(status_code=409, detail="Snapshot context has changed since draft was prepared")

            decision = Decision(
                decision_id=payload.decision_id,
                context_id=snapshot.context.context_id,
                head_sha=snapshot.context.head_sha,
                contract_digest=snapshot.context.integrity_digest,
                verdict=payload.verdict,
                directive=payload.directive,
                supersedes=current_tips,
                rationale=payload.rationale,
                source="dashboard",
            )
            body = render_decision(decision)

            # Durable serialize
            try:
                state = self.journal.record_attempt(decision_id=payload.decision_id, repository=repo_name, pr_number=pr_number, publisher_id=publisher_id, payload_digest=hashlib.sha256(body.encode("utf-8")).hexdigest())
            except ValueError as e:
                raise HTTPException(status_code=409, detail=str(e))

            if state in ("confirmed-published", "outcome-unknown"):
                state_data = self.journal.get_state(payload.decision_id)
                if state_data:
                    cur_state, gh_id = state_data
                    if cur_state == "outcome-unknown":
                        # Attempt reconciliation
                        try:
                            comments = gh_client.pulls.list_review_comments(owner=repo_name.split("/")[0], repo=repo_name.split("/")[1], pull_number=pr_number)
                            for c in comments:
                                if c.user.id == publisher_id and f'"decision_id": "{payload.decision_id}"' in c.body:
                                    self.journal.transition_state(payload.decision_id, "outcome-unknown", "confirmed-published", c.id)
                                    import asyncio

                                    if self.engine._loop:
                                        asyncio.run_coroutine_threadsafe(self.engine.invalidate_entity(repo_name, "pr", pr_number), self.engine._loop)
                                    return {"status": "published-awaiting-processing", "github_comment_id": c.id}
                        except Exception:
                            pass

                    if cur_state == "confirmed-published":
                        return {"status": "published-awaiting-processing", "github_comment_id": gh_id}

                    return {"status": cur_state, "github_comment_id": gh_id}
                return {"status": "error", "github_comment_id": None}

            # Try sending
            if not self.journal.transition_state(payload.decision_id, "prepared", "sending"):
                # Transition failed, which means another request is already processing it
                cur_state, gh_id = self.journal.get_state(payload.decision_id) or ("error", None)
                if cur_state == "sending":
                    # REQ-008: No blind POST while prior outcome remains unknown.
                    # If it's sending, we must not POST again.
                    return {"status": "outcome-unknown", "github_comment_id": None}
                return {"status": cur_state, "github_comment_id": gh_id}

            try:
                root_id = snapshot.context.root_comment_id
                url = f"https://api.github.com/repos/{repo_name}/pulls/{pr_number}/comments/{root_id}/replies"

                async with httpx.AsyncClient() as client:
                    res = await client.post(
                        url,
                        headers={
                            "Authorization": f"token {token}",
                            "Accept": "application/vnd.github.v3+json",
                            "X-GitHub-Api-Version": "2022-11-28",
                        },
                        json={"body": body},
                        timeout=10.0,
                    )
                    res.raise_for_status()

                gh_id = res.json().get("id")
                if not gh_id:
                    raise ValueError("No comment ID in response")

                self.journal.transition_state(payload.decision_id, "sending", "confirmed-published", gh_id)

                import asyncio

                if self.engine._loop:
                    asyncio.run_coroutine_threadsafe(self.engine.invalidate_entity(repo_name, "pr", pr_number), self.engine._loop)

                return {"status": "published-awaiting-processing", "github_comment_id": gh_id}
            except httpx.HTTPError as e:
                self.journal.transition_state(payload.decision_id, "sending", "outcome-unknown")
                return {"status": "outcome-unknown", "detail": str(e)}

        @self.router.get("/status/{decision_id}")
        async def get_status(req: Request, decision_id: str):
            session, config = require_auth(req, self)

            state_data = self.journal.get_state(decision_id)
            if not state_data:
                raise HTTPException(status_code=404, detail="Decision not found")

            state, gh_id = state_data
            if state == "outcome-unknown":
                repo_name = session.repository
                cur = self.journal._db.cursor()
                cur.execute("SELECT pr_number FROM publications WHERE decision_id = ?", (decision_id,))
                row = cur.fetchone()
                if row:
                    pr_number = row[0]
                    try:
                        gh_client = get_ghapi_client(self.engine.github.token)
                        comments = gh_client.pulls.list_review_comments(owner=repo_name.split("/")[0], repo=repo_name.split("/")[1], pull_number=pr_number)
                        cur.execute("SELECT publisher_id FROM publications WHERE decision_id = ?", (decision_id,))
                        pub_row = cur.fetchone()
                        if pub_row:
                            publisher_id = pub_row[0]
                            for c in comments:
                                if c.user.id == publisher_id and f'"decision_id": "{decision_id}"' in c.body:
                                    self.journal.transition_state(decision_id, "outcome-unknown", "confirmed-published", c.id)
                                state = "confirmed-published"
                                gh_id = c.id
                                break
                    except Exception:
                        pass

            return StatusResponse(decision_id=decision_id, state=state, github_comment_id=gh_id)


def init_dashboard_adjudication(app, engine, repo_name: str):
    from .llm_backend_config import get_dashboard_adjudication_config

    def config_factory(repo: str):
        return get_dashboard_adjudication_config(repo)

    service = AdjudicationService(config_factory, engine)
    app.include_router(service.router, prefix="/dashboard-api/adjudicate")
