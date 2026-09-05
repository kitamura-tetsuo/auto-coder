"""Publish adversarial verdicts with the dedicated GitHub App identity."""

from __future__ import annotations

import re
import threading
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import httpx
import jwt

from .adversarial_validator import (
    AdversarialValidationFinding,
    AdversarialValidationResult,
    TestOracleGap,
    format_adversarial_finding_comment,
    format_adversarial_review_summary,
    format_change_provenance_clarification,
    format_change_provenance_disposition,
    format_test_oracle_gap_comment,
)
from .llm_backend_config import deep_merge_config_dict, get_active_repo_name, resolve_repo_override_path
from .logger_config import get_logger
from .utils import is_same_github_login

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


@dataclass(frozen=True)
class ReviewerAppIdentity:
    """The dedicated reviewer App's own bot identity, resolved via its credentials."""

    login: str = ""
    app_id: int = 0

    def matches_login(self, candidate_login: Optional[str]) -> bool:
        """Return whether candidate_login matches this identity, ignoring '[bot]' suffix."""
        return is_same_github_login(self.login, candidate_login)


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
        self._identity: Optional[ReviewerAppIdentity] = None
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

    def get_identity(self) -> ReviewerAppIdentity:
        """Resolve the reviewer App's own bot login/id from its own credentials.

        This is deliberately independent of any string configured by the
        caller: the login is only accepted if the App's private key can
        authenticate a JWT and GitHub resolves that JWT's `/app` identity,
        so a lookalike review cannot be mistaken for an authoritative one.
        """
        with self._lock:
            if self._identity is not None:
                return self._identity
            app_jwt = self._jwt()
            info = self._request("GET", "/app", app_jwt).json()
            slug = info.get("slug") if isinstance(info, dict) else None
            app_id = info.get("id") if isinstance(info, dict) else None
            if not isinstance(slug, str) or not slug or not isinstance(app_id, int):
                raise RuntimeError("Reviewer GitHub App identity could not be resolved")
            identity = ReviewerAppIdentity(login=f"{slug}[bot]", app_id=app_id)
            self._identity = identity
            return identity

    def publish(self, repo_name: str, pr_number: int, validated_head_sha: str, result: AdversarialValidationResult) -> ReviewPublicationResult:
        """Submit a native review, failing closed on auth, head races, or API errors."""
        event = "APPROVE" if result.allows_auto_merge else "REQUEST_CHANGES" if result.needs_fix or result.needs_tests else "COMMENT"
        try:
            token = self._installation_token(repo_name)
            current_pr = self._request("GET", f"/repos/{repo_name}/pulls/{pr_number}", token).json()
            current_sha = current_pr.get("head", {}).get("sha") if isinstance(current_pr, dict) else None
            if not validated_head_sha or current_sha != validated_head_sha:
                return ReviewPublicationResult(False, event, "Pull request head changed after adversarial validation")
            comments: list[dict[str, object]] = []
            file_level_clarification: Optional[dict[str, object]] = None
            published_gap_ids = self._published_test_oracle_gap_ids(repo_name, pr_number, token) if result.open_test_oracle_gaps else set()
            gaps_to_publish = [gap for gap in result.open_test_oracle_gaps if gap.gap_id not in published_gap_ids]
            if result.needs_fix or result.needs_tests or (result.unexplained_changes and result.publish_clarification_thread):
                changed_files: dict[str, object] = {}
                page = 1
                while True:
                    files_response = self._request("GET", f"/repos/{repo_name}/pulls/{pr_number}/files?per_page=100&page={page}", token).json()
                    if not isinstance(files_response, list):
                        raise RuntimeError("GitHub did not return changed files")
                    changed_files.update({item["filename"]: item.get("patch", "") for item in files_response if isinstance(item, dict) and isinstance(item.get("filename"), str)})
                    if len(files_response) < 100:
                        break
                    page += 1
                comments = [self._finding_comment(finding, changed_files) for finding in result.findings]
                comments.extend(self._test_oracle_gap_comment(gap, changed_files) for gap in gaps_to_publish)
                if result.unexplained_changes and result.publish_clarification_thread:
                    clarification_body = format_change_provenance_clarification(result.unexplained_changes)
                    unexplained_paths = [path for item in result.unexplained_changes for path in item.paths if path in changed_files]
                    anchor = next(
                        ((path, diff_anchor) for path in unexplained_paths for diff_anchor in [_first_diff_anchor(changed_files[path])] if diff_anchor is not None),
                        None,
                    )
                    if anchor is None:
                        anchor = next(((path, diff_anchor) for path, patch in changed_files.items() for diff_anchor in [_first_diff_anchor(patch)] if diff_anchor is not None), None)
                    if anchor is not None:
                        anchor_path, (anchor_line, anchor_side) = anchor
                        comments.append({"path": anchor_path, "body": clarification_body, "line": anchor_line, "side": anchor_side})
                    elif unexplained_paths:
                        file_level_clarification = {
                            "path": unexplained_paths[0],
                            "body": clarification_body,
                            "commit_id": validated_head_sha,
                            "subject_type": "file",
                        }
                    else:
                        raise ValueError("Change-provenance clarification must reference a changed file")

            # A standalone file-level review comment is the only supported REST
            # shape when no changed file exposes a represented diff line. Create
            # it before the durable verdict so a failed comment request cannot
            # leave a saved same-SHA result that suppresses the required thread.
            if file_level_clarification is not None:
                self._request(
                    "POST",
                    f"/repos/{repo_name}/pulls/{pr_number}/comments",
                    token,
                    json=file_level_clarification,
                )
            self._request(
                "POST",
                f"/repos/{repo_name}/pulls/{pr_number}/reviews",
                token,
                json={
                    "body": format_adversarial_review_summary(
                        result,
                        validated_head_sha,
                        attached_test_oracle_gap_count=len(gaps_to_publish),
                    ),
                    "event": event,
                    "commit_id": validated_head_sha,
                    **({"comments": comments} if comments else {}),
                },
            )
            for disposition in result.thread_dispositions:
                root_comment_id = result.provenance_thread_comment_ids.get(disposition.thread_id)
                if disposition.status == "ADDRESSED" or root_comment_id is None:
                    continue
                try:
                    self._request(
                        "POST",
                        f"/repos/{repo_name}/pulls/{pr_number}/comments/{root_comment_id}/replies",
                        token,
                        json={"body": format_change_provenance_disposition(disposition, validated_head_sha)},
                    )
                except Exception:
                    # The App-authored authoritative review above already carries
                    # the same concrete rationale/evidence. Never fall back to the
                    # ordinary GitHub credential for reviewer output.
                    logger.error(f"Dedicated reviewer GitHub App could not reply to provenance thread {disposition.thread_id}")
            return ReviewPublicationResult(True, event, "")
        except Exception:
            # Deliberately omit exception text: HTTP errors and auth libraries can
            # include credential-bearing request details.
            logger.error("Dedicated reviewer GitHub App could not publish the adversarial verdict")
            return ReviewPublicationResult(False, event, "Dedicated reviewer GitHub App publication failed")

    def _published_test_oracle_gap_ids(self, repo_name: str, pr_number: int, token: str) -> set[str]:
        """Return stable gap identities that already have a root review thread."""
        published: set[str] = set()
        page = 1
        marker = re.compile(r"^Gap identity: `([^`]+)`$", re.MULTILINE)
        while True:
            comments = self._request(
                "GET",
                f"/repos/{repo_name}/pulls/{pr_number}/comments?per_page=100&page={page}",
                token,
            ).json()
            if not isinstance(comments, list):
                raise RuntimeError("GitHub did not return pull-request review comments")
            for comment in comments:
                body = comment.get("body", "") if isinstance(comment, dict) else ""
                if isinstance(body, str):
                    match = marker.search(body)
                    if match:
                        published.add(match.group(1))
            if len(comments) < 100:
                return published
            page += 1

    @staticmethod
    def _finding_comment(finding: AdversarialValidationFinding, changed_files: dict[str, object]) -> dict[str, object]:
        """Build one review comment anchored to a line accepted by nested reviews."""
        return GitHubAppReviewer._anchored_comment(
            finding.anchor_path,
            finding.anchor_line,
            finding.anchor_side,
            finding.anchor_start_line,
            format_adversarial_finding_comment(finding),
            changed_files,
        )

    @staticmethod
    def _test_oracle_gap_comment(gap: TestOracleGap, changed_files: dict[str, object]) -> dict[str, object]:
        """Build one review thread that requests focused regression protection."""
        return GitHubAppReviewer._anchored_comment(
            gap.anchor_path,
            gap.anchor_line,
            gap.anchor_side,
            gap.anchor_start_line,
            format_test_oracle_gap_comment(gap),
            changed_files,
        )

    @staticmethod
    def _anchored_comment(
        anchor_path: str,
        anchor_line: Optional[int],
        anchor_side: str,
        anchor_start_line: Optional[int],
        body: str,
        changed_files: dict[str, object],
    ) -> dict[str, object]:
        """Build one review comment anchored to a line accepted by nested reviews."""
        if not anchor_path or anchor_path not in changed_files:
            raise ValueError("Every actionable finding must anchor to a changed file")
        comment: dict[str, object] = {
            "path": anchor_path,
            "body": body,
        }
        side = anchor_side.strip().upper()
        patch = changed_files[anchor_path]
        valid_lines = _diff_lines(patch if isinstance(patch, str) else "", side)
        if side in {"LEFT", "RIGHT"} and anchor_line in valid_lines:
            comment.update({"line": anchor_line, "side": side})
            if anchor_start_line in valid_lines and anchor_start_line != anchor_line and anchor_start_line < anchor_line:
                comment.update({"start_line": anchor_start_line, "start_side": side})
        else:
            fallback_anchor = _first_diff_anchor(patch if isinstance(patch, str) else "")
            if fallback_anchor is None:
                raise ValueError("Every nested review comment must anchor to a represented diff line")
            line, fallback_side = fallback_anchor
            comment.update({"line": line, "side": fallback_side})
        return comment


def _diff_lines(patch: str, side: str) -> set[int]:
    """Return line numbers represented on one side of a unified diff patch."""
    import re

    represented: set[int] = set()
    old_line = new_line = 0
    for raw_line in patch.splitlines():
        match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw_line)
        if match:
            old_line, new_line = int(match.group(1)), int(match.group(2))
            continue
        if raw_line.startswith("\\") or not raw_line or raw_line.startswith(("---", "+++")):
            continue
        prefix = raw_line[0]
        if prefix != "+":
            if side == "LEFT":
                represented.add(old_line)
            old_line += 1
        if prefix != "-":
            if side == "RIGHT":
                represented.add(new_line)
            new_line += 1
    return represented


def _first_diff_anchor(patch: object) -> Optional[tuple[int, str]]:
    """Choose a valid nested-review anchor, preferring the current-file side."""
    if not isinstance(patch, str):
        return None
    right_lines = _diff_lines(patch, "RIGHT")
    if right_lines:
        return min(right_lines), "RIGHT"
    left_lines = _diff_lines(patch, "LEFT")
    if left_lines:
        return min(left_lines), "LEFT"
    return None


def publish_adversarial_review(repo_name: str, pr_number: int, head_sha: str, result: AdversarialValidationResult) -> ReviewPublicationResult:
    """Load dedicated credentials and publish without touching the user client."""
    try:
        reviewer = GitHubAppReviewer(load_reviewer_app_config(repo_name=repo_name))
    except Exception:
        logger.error("Dedicated reviewer GitHub App configuration could not be loaded")
        return ReviewPublicationResult(False, "", "Dedicated reviewer GitHub App configuration is unavailable")
    return reviewer.publish(repo_name, pr_number, head_sha, result)


def resolve_reviewer_app_identity(repo_name: Optional[str] = None) -> ReviewerAppIdentity:
    """Resolve the dedicated reviewer App's own bot identity.

    Raises on any failure (missing config, unreachable API, malformed
    response) so merge-critical persisted-state lookups can fail closed
    instead of silently treating an unresolved identity as "no review".
    """
    reviewer = GitHubAppReviewer(load_reviewer_app_config(repo_name=repo_name))
    return reviewer.get_identity()
