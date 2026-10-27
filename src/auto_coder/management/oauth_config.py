"""Configuration for the management OAuth credential authority."""

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from .action_scopes import KNOWN_SCOPES, READ_SCOPE, validate_scope_set

SUPPORTED_SCOPE = READ_SCOPE


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    redirect_uris: tuple[str, ...]
    token_endpoint_auth_method: str = "none"
    allowed_scopes: tuple[str, ...] = (READ_SCOPE,)

    def __post_init__(self) -> None:
        if not isinstance(self.client_id, str) or not self.client_id.strip():
            raise ValueError("client_id must be a non-empty string")
        if self.token_endpoint_auth_method != "none" or not self.redirect_uris:
            raise ValueError("invalid public OAuth client")
        for uri in self.redirect_uris:
            if not isinstance(uri, str) or not uri:
                raise ValueError("redirect_uris must contain non-empty strings")
            parsed = urlsplit(uri)
            loopback = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"} and parsed.port is not None
            if parsed.fragment or not parsed.netloc or (parsed.scheme != "https" and not loopback):
                raise ValueError("redirect URI must be HTTPS or an explicit-port HTTP loopback URI")
        validate_scope_set(self.allowed_scopes)


@dataclass(frozen=True)
class OAuthSettings:
    database_path: Path
    issuer: str
    firebase_project_id: str
    clients: tuple[OAuthClient, ...]
    access_lifetime: int = 900
    grant_lifetime: int = 30 * 24 * 60 * 60
    firebase_web_config: dict[str, str] = field(default_factory=dict)
    enabled_scopes: tuple[str, ...] = (READ_SCOPE,)

    def __post_init__(self) -> None:
        parsed = urlsplit(self.issuer)
        localhost = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        if parsed.query or parsed.fragment or parsed.path not in {"", "/"} or not parsed.netloc:
            raise ValueError("issuer must be an origin")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and localhost):
            raise ValueError("issuer must use HTTPS outside the localhost profile")
        if not self.firebase_project_id or not self.clients or len({c.client_id for c in self.clients}) != len(self.clients):
            raise ValueError("project and uniquely identified clients are required")
        if not 0 < self.access_lifetime <= 900 or not 0 < self.grant_lifetime <= 30 * 24 * 60 * 60:
            raise ValueError("OAuth lifetime exceeds the supported maximum")
        validate_scope_set(self.enabled_scopes)
        if any(not set(client.allowed_scopes) <= KNOWN_SCOPES for client in self.clients):
            raise ValueError("client contains an unknown scope")

    @property
    def canonical_issuer(self) -> str:
        return self.issuer.rstrip("/")

    @property
    def resource(self) -> str:
        return f"{self.canonical_issuer}/mcp"

    def client(self, client_id: str) -> OAuthClient | None:
        return next((client for client in self.clients if client.client_id == client_id), None)


@dataclass(frozen=True)
class ManagementSettings:
    """Validated settings for the standalone management process."""

    state_root: Path
    oauth: OAuthSettings
    bind_host: str = "127.0.0.1"
    port: int = 8081
    local_development: bool = False
    allowed_browser_origins: tuple[str, ...] = ()
    operating_policy_path: Path | None = None
    operation_state_root: Path | None = None
    source_repository: str | None = None
    source_evidence_token_file: Path | None = None

    def __post_init__(self) -> None:
        loopback = self.bind_host in {"127.0.0.1", "::1", "localhost"}
        is_http = urlsplit(self.oauth.canonical_issuer).scheme.lower() == "http"
        if is_http and not (self.local_development and loopback):
            raise ValueError("HTTP public origins require the loopback development profile")
        if not is_http and self.local_development:
            raise ValueError("the development profile requires a loopback HTTP origin")
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if self.oauth.database_path.parent != self.state_root:
            raise ValueError("OAuth state must be stored directly in state_root")
        if self.operating_policy_path is not None and not self.operating_policy_path.is_absolute():
            raise ValueError("operating policy must use an absolute operator-owned path")
        if self.operation_state_root is not None and not self.operation_state_root.is_absolute():
            raise ValueError("operation state root must be absolute")
        if self.source_repository is not None and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.source_repository) is None:
            raise ValueError("source repository must be an exact GitHub owner/name")
        if self.source_evidence_token_file is not None and not self.source_evidence_token_file.is_absolute():
            raise ValueError("source evidence token must use an absolute operator-owned path")


def load_management_settings(path: Path) -> ManagementSettings:
    """Load management YAML without accepting request-derived authority values."""

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("management configuration must be a mapping")

    def required_string(name: str) -> str:
        value = raw.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
        return value

    state_root = Path(required_string("state_root")).expanduser().resolve()
    clients_raw = raw["clients"]
    if not isinstance(clients_raw, list):
        raise ValueError("clients must be a list")
    clients_list = []
    for item in clients_raw:
        if not isinstance(item, dict):
            raise ValueError("each client must be a mapping")
        client_id = item.get("client_id")
        if not isinstance(client_id, str) or not client_id.strip():
            raise ValueError("client_id must be a non-empty string")
        redirect_uris = item.get("redirect_uris")
        if not isinstance(redirect_uris, list):
            raise ValueError("redirect_uris must be a list")
        allowed = item.get("allowed_scopes", [READ_SCOPE])
        if not isinstance(allowed, list) or any(not isinstance(scope, str) for scope in allowed):
            raise ValueError("allowed_scopes must be a list of strings")
        clients_list.append(OAuthClient(client_id, tuple(redirect_uris), allowed_scopes=validate_scope_set(tuple(allowed))))
    clients = tuple(clients_list)

    bind_host = raw.get("bind_host", "127.0.0.1")
    if not isinstance(bind_host, str) or not bind_host.strip():
        raise ValueError("bind_host must be a non-empty string")
    port = raw.get("port", 8081)
    if type(port) is not int:
        raise ValueError("port must be an integer")
    local_development = raw.get("local_development", False)
    if not isinstance(local_development, bool):
        raise ValueError("local_development must be a boolean")
    browser_origins = raw.get("allowed_browser_origins", [])
    if not isinstance(browser_origins, list) or any(not isinstance(origin, str) for origin in browser_origins):
        raise ValueError("allowed_browser_origins must be a list of strings")
    enabled = raw.get("enabled_scopes", [READ_SCOPE])
    if not isinstance(enabled, list) or any(not isinstance(scope, str) for scope in enabled):
        raise ValueError("enabled_scopes must be a list of strings")
    oauth = OAuthSettings(
        state_root / "oauth.sqlite3",
        required_string("public_origin"),
        required_string("firebase_project_id"),
        clients,
        firebase_web_config={str(k): str(v) for k, v in dict(raw.get("firebase_web_config", {})).items()},
        enabled_scopes=validate_scope_set(tuple(enabled)),
    )

    def optional_path(name: str) -> Path | None:
        value = raw.get(name)
        if value is None:
            return None
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise ValueError(f"{name} must be an absolute path")
        return Path(value)

    return ManagementSettings(
        state_root,
        oauth,
        bind_host,
        port,
        local_development,
        tuple(browser_origins),
        optional_path("operating_policy_path"),
        optional_path("operation_state_root"),
        raw.get("source_repository") if raw.get("source_repository") is None or isinstance(raw.get("source_repository"), str) else required_string("source_repository"),
        optional_path("source_evidence_token_file"),
    )
