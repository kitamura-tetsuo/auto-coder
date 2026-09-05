"""One-way publication of the local Codex login state to GitHub Actions."""

import json
import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from .llm_backend_config import load_app_config_data
from .logger_config import get_logger

logger = get_logger(__name__)

DEFAULT_SECRET_NAME = "CODEX_AUTH_JSON"
_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SECRET_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_KNOWN_FIELDS = {"enabled", "token", "repository", "secret_name"}


class SyncOutcome(str, Enum):
    """Observable, non-secret result of an optional synchronization attempt."""

    DISABLED = "disabled"
    CONFIGURATION_ERROR = "configuration_error"
    LOCAL_AUTH_ERROR = "local_auth_error"
    PERMISSION_ERROR = "permission_error"
    PUBLICATION_ERROR = "publication_error"
    PUBLISHED = "published"


@dataclass(frozen=True)
class GitHubSecretsConfig:
    """Strict configuration for the dedicated Actions Secrets boundary."""

    enabled: bool = False
    token: str = ""
    repository: str = ""
    secret_name: str = DEFAULT_SECRET_NAME


@dataclass(frozen=True)
class SyncResult:
    outcome: SyncOutcome
    repository: str = ""
    secret_name: str = DEFAULT_SECRET_NAME
    source_path: str = ""


def _load_config(config_path: Optional[str]) -> GitHubSecretsConfig:
    data = load_app_config_data(config_path=config_path)
    raw = data.get("github_secrets")
    if raw is None:
        return GitHubSecretsConfig()
    if not isinstance(raw, dict):
        raise ValueError("[github_secrets] must be a TOML table")

    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("[github_secrets].enabled must be a boolean")
    if not enabled:
        return GitHubSecretsConfig()

    unknown = set(raw) - _KNOWN_FIELDS
    if unknown:
        raise ValueError("enabled [github_secrets] contains unknown fields")
    token = raw.get("token")
    repository = raw.get("repository")
    secret_name = raw.get("secret_name", DEFAULT_SECRET_NAME)
    if not isinstance(token, str) or not token.strip():
        raise ValueError("enabled [github_secrets] requires a dedicated token")
    if not isinstance(repository, str) or not _REPOSITORY_PATTERN.fullmatch(repository):
        raise ValueError("enabled [github_secrets] requires repository in owner/name format")
    if repository.startswith(("./", "../")) or "/." in repository:
        raise ValueError("enabled [github_secrets] requires repository in owner/name format")
    if not isinstance(secret_name, str) or not _SECRET_NAME_PATTERN.fullmatch(secret_name):
        raise ValueError("[github_secrets].secret_name is invalid")
    return GitHubSecretsConfig(True, token, repository, secret_name)


def _auth_path() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    return Path(codex_home).expanduser() / "auth.json" if codex_home else Path.home() / ".codex" / "auth.json"


def _read_valid_auth(path: Path) -> str:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("local Codex auth is missing or unreadable") from exc
    if not content.strip():
        raise ValueError("local Codex auth is empty")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("local Codex auth is malformed") from exc
    if not isinstance(parsed, dict):
        raise ValueError("local Codex auth is not a JSON object")
    return content


def synchronize_codex_auth_secret(config_path: Optional[str] = None) -> SyncResult:
    """Publish exact local auth content, while keeping this optional boundary isolated."""
    try:
        config = _load_config(config_path)
    except (OSError, ValueError) as exc:
        logger.warning(f"Codex auth secret synchronization configuration error: {exc}")
        return SyncResult(SyncOutcome.CONFIGURATION_ERROR)
    if not config.enabled:
        return SyncResult(SyncOutcome.DISABLED)

    source = _auth_path()
    result_fields = {"repository": config.repository, "secret_name": config.secret_name, "source_path": str(source)}
    try:
        content = _read_valid_auth(source)
    except ValueError as exc:
        logger.warning(f"Codex auth secret synchronization local-auth error for {source}: {exc}")
        return SyncResult(SyncOutcome.LOCAL_AUTH_ERROR, **result_fields)

    # Import here so disabled configurations do not initialize GitHub publication code.
    from .util.gh_cache import ActionsSecretPermissionError, ActionsSecretPublisher

    try:
        ActionsSecretPublisher(config.token).set_repository_secret(config.repository, config.secret_name, content)
    except ActionsSecretPermissionError:
        logger.warning(f"Codex auth secret synchronization permission failure for {config.repository}/{config.secret_name}")
        return SyncResult(SyncOutcome.PERMISSION_ERROR, **result_fields)
    except Exception:
        # Never expose transport exception text: it is outside this module's redaction boundary.
        logger.warning(f"Codex auth secret synchronization publication failure for {config.repository}/{config.secret_name}")
        return SyncResult(SyncOutcome.PUBLICATION_ERROR, **result_fields)

    logger.info(f"Published local Codex auth to Actions secret {config.repository}/{config.secret_name} from {source}")
    return SyncResult(SyncOutcome.PUBLISHED, **result_fields)
