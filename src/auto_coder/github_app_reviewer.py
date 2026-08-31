"""Publish adversarial verdicts with the dedicated GitHub App identity."""

from __future__ import annotations

import threading
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import httpx
import jwt

from .adversarial_validator import AdversarialValidationResult
from .llm_backend_config import deep_merge_config_dict, get_active_repo_name, resolve_repo_override_path
from .logger_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ReviewerAppConfig:
    """Non-secret reviewer App settings and the external private-key location."""

    app_id: str = ""
    client_id: str = ""
    private_key_path: Path = Path.home() / ".auto-coder" / "auto-coder-reviewer.pem"


@dataclass(frozen=True)
class ReviewPublicationResult:
    """Observable outcome of publishing a native GitHub review."""

    success: bool = False
    event: str = ""
    reason: str = ""


@dataclass
class _CachedToken:
    value: str = ""
    expires_at: float = 0.0


def load_reviewer_app_config(
    config_path: Optional[Path] = None,
    home: Optional[Path] = None,
    repo_name: Optional[str] = None,
) -> ReviewerAppConfig:
    """Load ``[github-app-auto-coder-reviewer]`` without reading the PEM into config."""
    base_home = home or Path.home()
    path = config_path or base_home / ".auto-coder" / "config.toml"
    try:
        with path.open("rb") as stream:
            base_data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("Reviewer GitHub App configuration is unavailable or invalid") from exc
    if not isinstance(base_data, dict):
        raise ValueError("Reviewer GitHub App configuration section is invalid")

    effective_repo = repo_name if repo_name is not None else get_active_repo_name()
    if effective_repo:
        override_path_str = resolve_repo_override_path(
            effective_repo,
            override_root_dir=str(base_home / ".auto-coder"),
            filename="config.toml",
        )
        if override_path_str:
            override_path = Path(override_path_str)
            if override_path.exists():
                try:
                    with override_path.open("rb") as stream:
                        override_data = tomllib.load(stream)
                except (OSError, tomllib.TOMLDecodeError) as exc:
                    raise ValueError(f"Error loading repository configuration override from {override_path}: {exc}") from exc
                if not isinstance(override_data, dict):
                    raise ValueError(f"Repository configuration override at {override_path} must be a TOML table")
                base_data = deep_merge_config_dict(base_data, override_data)

    section = base_data.get("github-app-auto-coder-reviewer", {})
    if not isinstance(section, dict):
        raise ValueError("Reviewer GitHub App configuration section is invalid")
    app_id = str(section.get("app_id", "")).strip()
    client_id = str(section.get("client_id", "")).strip()
    if not app_id or not app_id.isdigit():
        raise ValueError("Reviewer GitHub App app_id is missing or invalid")
    return ReviewerAppConfig(
        app_id=app_id,
        client_id=client_id,
        private_key_path=base_home / ".auto-coder" / "auto-coder-reviewer.pem",
    )


class GitHubAppReviewer:
    """A narrowly scoped client that can only resolve installations and submit reviews."""

    def __init__(
        self,
        config: ReviewerAppConfig,
        *,
        api_url: str = "https://api.github.com",
        client: Optional[httpx.Client] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._api_url = api_url.rstrip("/")
        self._client = client or httpx.Client(timeout=30.0)
        self._clock = clock
        self._tokens: dict[str, _CachedToken] = {}
        self._lock = threading.Lock()

    def _jwt(self) -> str:
        try:
            private_key = self._config.private_key_path.read_text(encoding="utf-8")
            now = int(self._clock())
            encoded = jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self._config.app_id}, private_key, algorithm="RS256")
            return str(encoded)
        except Exception as exc:
            raise RuntimeError("Could not create reviewer GitHub App authentication") from exc

    def _request(self, method: str, path: str, token: str, json: Optional[dict[str, object]] = None) -> httpx.Response:
        response = self._client.request(
            method,
            f"{self._api_url}{path}",
            headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"},
            json=json,
        )
        response.raise_for_status()
        return response

    def _installation_token(self, repo_name: str) -> str:
        with self._lock:
            cached = self._tokens.get(repo_name)
            if cached and cached.expires_at - self._clock() > 60:
                return cached.value

            app_jwt = self._jwt()
            installation = self._request("GET", f"/repos/{repo_name}/installation", app_jwt).json()
            installation_id = installation.get("id") if isinstance(installation, dict) else None
            if not isinstance(installation_id, int):
                raise RuntimeError("Reviewer GitHub App has no installation for the repository")
            token_data = self._request(
                "POST",
                f"/app/installations/{installation_id}/access_tokens",
                app_jwt,
                json={"permissions": {"pull_requests": "write"}},
            ).json()
            token = token_data.get("token") if isinstance(token_data, dict) else None
            expires_at = token_data.get("expires_at") if isinstance(token_data, dict) else None
            if not isinstance(token, str) or not token or not isinstance(expires_at, str):
                raise RuntimeError("GitHub did not return a reviewer installation token")
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
            self._tokens[repo_name] = _CachedToken(token, expiry)
            return token

    @staticmethod
    def _body(result: AdversarialValidationResult) -> str:
        lines = ["## Adversarial validation", "", f"**Verdict:** {result.result.strip().upper()}", "", result.summary or "No summary was provided."]
        for index, finding in enumerate(result.findings, 1):
            lines.extend(
                [
                    "",
                    f"### Finding {index}",
                    f"- **Violated requirement:** {finding.violated_requirement}",
                    f"- **Counterexample:** {finding.counterexample}",
                    f"- **Test gap:** {finding.test_gap}",
                    f"- **Suggested regression scenario:** {finding.suggested_regression_scenario}",
                ]
            )
        if result.specification_gaps:
            lines.extend(["", "### Specification gaps", "", "These are not proven defects, and Auto-Coder did not choose a policy. Review of defined requirements continued. Automatic merge is disabled while a gap remains; a human may merge manually and handle specification work separately."])
            for index, gap in enumerate(result.specification_gaps, 1):
                lines.extend(["", f"#### Gap {index}: {gap.question}", f"- **Why the Issue is insufficient:** {gap.why_existing_issue_is_insufficient}", f"- **Observed case:** {gap.observed_case}", f"- **Affected scope:** {gap.affected_scope}"])
        return "\n".join(lines)

    def publish(self, repo_name: str, pr_number: int, validated_head_sha: str, result: AdversarialValidationResult) -> ReviewPublicationResult:
        """Submit a native review, failing closed on auth, head races, or API errors."""
        event = "APPROVE" if result.auto_merge_allowed else "REQUEST_CHANGES" if result.needs_fix else "COMMENT"
        try:
            token = self._installation_token(repo_name)
            current_pr = self._request("GET", f"/repos/{repo_name}/pulls/{pr_number}", token).json()
            current_sha = current_pr.get("head", {}).get("sha") if isinstance(current_pr, dict) else None
            if not validated_head_sha or current_sha != validated_head_sha:
                return ReviewPublicationResult(False, event, "Pull request head changed after adversarial validation")
            self._request(
                "POST",
                f"/repos/{repo_name}/pulls/{pr_number}/reviews",
                token,
                json={"body": self._body(result), "event": event, "commit_id": validated_head_sha},
            )
            return ReviewPublicationResult(True, event, "")
        except Exception:
            # Deliberately omit exception text: HTTP errors and auth libraries can
            # include credential-bearing request details.
            logger.error("Dedicated reviewer GitHub App could not publish the adversarial verdict")
            return ReviewPublicationResult(False, event, "Dedicated reviewer GitHub App publication failed")


def publish_adversarial_review(repo_name: str, pr_number: int, head_sha: str, result: AdversarialValidationResult) -> ReviewPublicationResult:
    """Load dedicated credentials and publish without touching the user client."""
    try:
        reviewer = GitHubAppReviewer(load_reviewer_app_config(repo_name=repo_name))
    except Exception:
        logger.error("Dedicated reviewer GitHub App configuration could not be loaded")
        return ReviewPublicationResult(False, "", "Dedicated reviewer GitHub App configuration is unavailable")
    return reviewer.publish(repo_name, pr_number, head_sha, result)
