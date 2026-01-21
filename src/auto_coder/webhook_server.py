import asyncio
import hashlib
import hmac
from typing import Any, Dict, List, Optional, Union

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from .automation_config import Candidate
from .automation_engine import AutomationEngine
from .logger_config import get_logger

logger = get_logger(__name__)


class SentryWebhookPayload(BaseModel):
    message: Optional[str] = None
    project_name: Optional[str] = None
    project: Optional[str] = None
    level: Optional[str] = None
    url: Optional[str] = None
    web_url: Optional[str] = None
    event: Optional[Dict[str, Any]] = None
    data: Optional[Dict[str, Any]] = None


def verify_github_signature(payload: bytes, secret: str, signature: Optional[str]):
    if not signature:
        raise HTTPException(status_code=403, detail="Missing signature")

    # Signature format: sha256=...
    if not signature.startswith("sha256="):
        raise HTTPException(status_code=403, detail="Invalid signature format")

    sha_name, signature_hash = signature.split("=")
    if sha_name != "sha256":
        raise HTTPException(status_code=501, detail="Operation not supported")

    mac = hmac.new(secret.encode(), msg=payload, digestmod=hashlib.sha256)
    if not hmac.compare_digest(mac.hexdigest(), signature_hash):
        raise HTTPException(status_code=403, detail="Invalid signature")


def verify_sentry_signature(payload: bytes, secret: str, signature: Optional[str]):
    if not signature:
        raise HTTPException(status_code=403, detail="Missing signature")

    mac = hmac.new(secret.encode(), msg=payload, digestmod=hashlib.sha256)
    if not hmac.compare_digest(mac.hexdigest(), signature):
        raise HTTPException(status_code=403, detail="Invalid signature")


async def process_sentry_payload(payload: SentryWebhookPayload, engine: AutomationEngine, repo_name: str):
    try:
        data = payload.data or {}
        event = data.get("event", {}) if data else (payload.event or {})

        # Fallback extraction
        message = payload.message or event.get("title") or "Sentry Error"
        project_name = payload.project_name or payload.project or "Sentry"
        level = payload.level or event.get("level") or "error"
        url = payload.url or payload.web_url or ""

        title = f"[Sentry] {message}"
        if len(title) > 200:
            title = title[:197] + "..."

        body = f"**Sentry Error Detected**\n\n"
        body += f"**Project:** {project_name}\n"
        body += f"**Level:** {level}\n"
        if url:
            body += f"**URL:** {url}\n"

        body += "\n\n*This issue was automatically created by Auto-Coder webhook daemon.*"

        logger.info(f"Creating issue for Sentry error: {title}")

        loop = asyncio.get_running_loop()
        issue = await loop.run_in_executor(None, lambda: engine.github.create_issue(repo_name, title, body, labels=["sentry", "bug", "urgent"]))

        if issue:
            issue_details = await loop.run_in_executor(None, lambda: engine.github.get_issue_details(issue))

            candidate = Candidate(type="issue", data=issue_details, priority=3, issue_number=issue_details.get("number"))  # Urgent

            await engine.queue.put(candidate)
            logger.info(f"Queued Sentry issue #{candidate.issue_number}")

    except Exception as e:
        logger.error(f"Failed to process Sentry payload: {e}")


async def process_github_payload(event_type: Optional[str], payload: Dict[str, Any], engine: AutomationEngine, repo_name: str):
    try:
        if event_type == "pull_request":
            action = payload.get("action")
            if action in ["opened", "reopened", "synchronize", "ready_for_review"]:
                pr_data_raw = payload.get("pull_request")
                if pr_data_raw:
                    pr_number = pr_data_raw.get("number")
                    logger.info(f"Processing PR #{pr_number} event: {action}")

                    loop = asyncio.get_running_loop()
                    try:
                        pr_obj = await loop.run_in_executor(None, lambda: engine.github.get_pull_request(repo_name, pr_number))

                        if pr_obj:
                            pr_details = await loop.run_in_executor(None, lambda: engine.github.get_pr_details(pr_obj))

                            candidate = Candidate(type="pr", data=pr_details, priority=0)
                            await engine.queue.put(candidate)
                            logger.info(f"Queued PR #{pr_number}")
                    except Exception as e:
                        logger.error(f"Failed to fetch/queue PR #{pr_number}: {e}")

        elif event_type == "workflow_run":
            action = payload.get("action")
            workflow_run = payload.get("workflow_run", {})
            conclusion = workflow_run.get("conclusion")

            if action == "completed" and conclusion == "failure":
                pull_requests = workflow_run.get("pull_requests", [])
                head_branch = workflow_run.get("head_branch")

                logger.info(f"Processing failed workflow run on {head_branch}")

                if pull_requests:
                    for pr in pull_requests:
                        pr_number = pr.get("number")
                        loop = asyncio.get_running_loop()
                        try:
                            pr_obj = await loop.run_in_executor(None, lambda: engine.github.get_pull_request(repo_name, pr_number))
                            if pr_obj:
                                pr_details = await loop.run_in_executor(None, lambda: engine.github.get_pr_details(pr_obj))
                                candidate = Candidate(type="pr", data=pr_details, priority=3)  # High priority for failing CI
                                await engine.queue.put(candidate)
                                logger.info(f"Queued failing PR #{pr_number}")
                        except Exception as e:
                            logger.error(f"Failed to fetch/queue PR #{pr_number}: {e}")

    except Exception as e:
        logger.error(f"Failed to process GitHub payload: {e}")


def create_app(engine: AutomationEngine, repo_name: str, github_secret: Optional[str] = None, sentry_secret: Optional[str] = None) -> FastAPI:
    app = FastAPI(title="Auto-Coder Daemon")

    @app.get("/")
    async def root():
        return {"status": "running", "repo": repo_name}

    @app.post("/hooks/sentry")
    async def sentry_hook(request: Request, background_tasks: BackgroundTasks):
        if sentry_secret:
            signature = request.headers.get("Sentry-Hook-Signature")
            body = await request.body()
            verify_sentry_signature(body, sentry_secret, signature)

        payload_dict = await request.json()
        payload = SentryWebhookPayload(**payload_dict)
        background_tasks.add_task(process_sentry_payload, payload, engine, repo_name)
        return {"status": "received"}

    @app.post("/hooks/github")
    async def github_hook(request: Request, background_tasks: BackgroundTasks):
        event_type = request.headers.get("X-GitHub-Event")

        if github_secret:
            signature = request.headers.get("X-Hub-Signature-256")
            body = await request.body()
            verify_github_signature(body, github_secret, signature)

        payload = await request.json()
        background_tasks.add_task(process_github_payload, event_type, payload, engine, repo_name)
        return {"status": "received"}

    return app
