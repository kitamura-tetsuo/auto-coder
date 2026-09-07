import asyncio
import hashlib
import hmac
import time
from collections.abc import Mapping
from typing import Any, Dict, Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from .automation_engine import AutomationEngine
from .dashboard import init_dashboard
from .entity_invalidation import ISSUE_STABILIZATION_SECONDS, issue_stabilization_deadline
from .label_manager import LEGACY_AUTO_CODER_LABEL
from .logger_config import get_logger

logger = get_logger(__name__)

# Webhook actions whose payload carries the single label that was added/removed.
_LABEL_CHANGE_ACTIONS = {"labeled", "unlabeled"}
_DEPENDENCY_EVENTS = {"issue_dependencies", "sub_issues"}


def _endpoint_is_local(endpoint: Mapping[str, Any], repo_name: str) -> bool:
    """Validate an endpoint's repository identity without comparing global IDs."""
    repository = endpoint.get("repository")
    full_name = repository.get("full_name") if isinstance(repository, Mapping) else None
    if isinstance(full_name, str):
        return full_name.casefold() == repo_name.casefold()
    repository_url = endpoint.get("repository_url")
    if isinstance(repository_url, str):
        return repository_url.rstrip("/").casefold().endswith(f"/repos/{repo_name}".casefold())
    html_url = endpoint.get("html_url")
    return isinstance(html_url, str) and html_url.casefold().startswith(f"https://github.com/{repo_name}/issues/".casefold())


def _is_legacy_auto_coder_label_change(event_type: Optional[str], action: Optional[str], payload: Dict[str, Any]) -> bool:
    """Detect a labeled/unlabeled webhook delivery for the exact retired "@auto-coder" label.

    REQ-001/REQ-002 (FTR-1792): such deliveries must not create, enqueue, or
    advance a durable invalidation, regardless of who made the change, while
    every other label change (including near-misses like "auto-coder" or
    "@auto-coder-old") must continue through the normal invalidation and
    delivery-deduplication path unaffected.
    """
    if event_type not in ("issues", "pull_request") or action not in _LABEL_CHANGE_ACTIONS:
        return False
    label = payload.get("label")
    return isinstance(label, Mapping) and label.get("name") == LEGACY_AUTO_CODER_LABEL


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

    mac = hmac.new(secret.encode(), msg=payload, digestmod=hashlib.sha256)
    expected_signature = f"sha256={mac.hexdigest()}"

    if not hmac.compare_digest(expected_signature, signature):
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

            issue_number = issue_details.get("number")
            if isinstance(issue_number, int):
                created_at = issue_details.get("created_at")
                not_before = time.time() + ISSUE_STABILIZATION_SECONDS
                if isinstance(created_at, str):
                    deadline = issue_stabilization_deadline(created_at)
                    if deadline is None:
                        logger.warning(f"Invalid created_at for Sentry issue #{issue_number}; using receipt-anchored stabilization")
                    else:
                        not_before = deadline
                await engine.invalidate_entity(repo_name, "issue", issue_number, event_type="sentry", action="created", not_before=not_before)
                logger.info(f"Scheduled Sentry issue #{issue_number} after stabilization")

    except Exception as e:
        logger.error(f"Failed to process Sentry payload: {e}")


async def process_github_payload(
    event_type: Optional[str],
    payload: Dict[str, Any],
    engine: AutomationEngine,
    repo_name: str,
    delivery_id: Optional[str] = None,
) -> None:
    """Translate relevant webhook notifications into durable entity invalidations."""
    action = payload.get("action")
    identities: set[tuple[str, int]] = set()
    dependency_reevaluation = False
    entity_actions = {
        "pull_request": {"opened", "edited", "closed", "reopened", "synchronize", "converted_to_draft", "ready_for_review", "labeled", "unlabeled", "assigned", "unassigned"},
        "issues": {"opened", "edited", "closed", "reopened", "labeled", "unlabeled", "assigned", "unassigned", "deleted", "transferred"},
        "pull_request_review": {"submitted", "edited", "dismissed"},
        "pull_request_review_comment": {"created", "edited", "deleted"},
        "pull_request_review_thread": {"resolved", "unresolved"},
        "issue_comment": {"created", "edited", "deleted"},
    }
    if event_type in entity_actions and action in entity_actions[event_type] and not _is_legacy_auto_coder_label_change(event_type, action, payload):
        if event_type == "issues":
            entity, entity_type = payload.get("issue"), "issue"
        elif event_type == "issue_comment":
            entity = payload.get("issue")
            entity_type = "pr" if isinstance(entity, Mapping) and "pull_request" in entity else "issue"
        else:
            entity, entity_type = payload.get("pull_request"), "pr"
        number = entity.get("number") if isinstance(entity, Mapping) else None
        if isinstance(number, int):
            identities.add((entity_type, number))
        # Issue lifecycle, declarations, and readiness labels can change the
        # eligibility of Issues which never receive their own webhook.
        if event_type == "issues":
            changed_label = payload.get("label")
            dependency_reevaluation = action in {"opened", "closed", "reopened", "deleted", "transferred"}
            dependency_reevaluation = dependency_reevaluation or (action == "edited" and isinstance(payload.get("changes"), Mapping) and "body" in payload["changes"])
            dependency_reevaluation = dependency_reevaluation or (action in _LABEL_CHANGE_ACTIONS and isinstance(changed_label, Mapping) and changed_label.get("name") == "implementation-ready")
        if event_type == "pull_request" and action == "closed":
            engine.notify_pr_merged_or_closed()
    elif event_type in entity_actions and action in entity_actions[event_type]:
        logger.info(f"Ignoring {event_type} {action} webhook for the retired '{LEGACY_AUTO_CODER_LABEL}' label: no invalidation created")

    if event_type in _DEPENDENCY_EVENTS:
        dependency_reevaluation = True
        endpoint_names = ("blocked_issue", "blocking_issue") if event_type == "issue_dependencies" else ("parent_issue", "sub_issue")
        for endpoint_name in endpoint_names:
            endpoint = payload.get(endpoint_name)
            if not isinstance(endpoint, Mapping) or not _endpoint_is_local(endpoint, repo_name):
                continue
            number = endpoint.get("number")
            if isinstance(number, int) and not isinstance(number, bool) and number > 0:
                identities.add(("issue", number))

    if dependency_reevaluation:
        # This repository-scoped token is the durable promise to perform a
        # conservative authoritative scan.  It is persisted before HTTP 200,
        # so removed edges and downtime cannot erase reverse discovery.
        identities.add(("dependency", 1))

    completion_events = {("workflow_run", "completed"), ("workflow_job", "completed"), ("check_run", "completed"), ("check_suite", "completed")}
    if (event_type, action) in completion_events or event_type == "status":
        container_name: Optional[str] = {
            "workflow_run": "workflow_run",
            "workflow_job": "workflow_job",
            "check_run": "check_run",
            "check_suite": "check_suite",
        }.get(event_type or "")
        container: object
        if container_name is None:
            container = payload
        else:
            container = payload.get(container_name)
        if not isinstance(container, Mapping):
            container = {}
        pull_requests = container.get("pull_requests", [])
        if isinstance(pull_requests, list):
            for pull_request in pull_requests:
                number = pull_request.get("number") if isinstance(pull_request, Mapping) else None
                if isinstance(number, int):
                    identities.add(("pr", number))
        if not identities:
            sha = container.get("head_sha") or container.get("sha")
            if isinstance(sha, str) and sha:
                numbers = await asyncio.to_thread(engine.github.get_pull_request_numbers_for_commit, repo_name, sha)
                identities.update(("pr", number) for number in numbers)

    # Queue the scope token first. Its scan then coalesces with endpoint rows
    # from this delivery instead of redispatching an endpoint already handled.
    for entity_type, number in sorted(identities, key=lambda identity: (identity[0] != "dependency", identity)):
        changed_label = payload.get("label")
        urgent_admission = entity_type == "issue" and event_type == "issues" and action == "labeled" and isinstance(changed_label, Mapping) and changed_label.get("name") == "urgent"
        not_before = None
        if entity_type == "issue":
            issue = payload.get("issue")
            created_at = issue.get("created_at") if isinstance(issue, Mapping) else None
            if isinstance(created_at, str):
                not_before = issue_stabilization_deadline(created_at)
                if not_before is None:
                    logger.warning(f"Invalid created_at for issue #{number}; scheduling immediate authoritative reevaluation")
        elif entity_type == "dependency" and event_type == "issues" and action == "opened":
            issue = payload.get("issue")
            created_at = issue.get("created_at") if isinstance(issue, Mapping) else None
            if isinstance(created_at, str):
                not_before = issue_stabilization_deadline(created_at)
        invalidation_args = (repo_name, entity_type, number, delivery_id, event_type, action if isinstance(action, str) else None)
        if urgent_admission:
            accepted = await engine.invalidate_entity(
                *invalidation_args,
                not_before=not_before,
                urgent_admission=True,
            )
        elif not_before is not None:
            accepted = await engine.invalidate_entity(*invalidation_args, not_before=not_before)
        else:
            accepted = await engine.invalidate_entity(*invalidation_args)
        logger.info(f"{'Accepted' if accepted else 'Ignored duplicate'} invalidation for {entity_type} #{number}")


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
        delivery_id = request.headers.get("X-GitHub-Delivery")

        if github_secret:
            signature = request.headers.get("X-Hub-Signature-256")
            body = await request.body()
            verify_github_signature(body, github_secret, signature)

        payload = await request.json()
        repository = payload.get("repository") if isinstance(payload, dict) else None
        payload_repo = repository.get("full_name") if isinstance(repository, dict) else None
        if not isinstance(payload_repo, str) or payload_repo.casefold() != repo_name.casefold():
            raise HTTPException(status_code=403, detail="Repository is outside webhook scope")
        # Persistence is part of accepting a delivery, so it must finish before
        # returning 200 rather than being delegated to an in-memory task.
        await process_github_payload(event_type, payload, engine, repo_name, delivery_id)
        return {"status": "received"}

    init_dashboard(app, engine)

    return app
