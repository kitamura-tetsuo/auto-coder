"""Configuration for the management OAuth credential authority."""

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml

SUPPORTED_SCOPE = "auto-coder.read"


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    redirect_uris: tuple[str, ...]
    token_endpoint_auth_method: str = "none"

    def __post_init__(self) -> None:
        if not self.client_id or self.token_endpoint_auth_method != "none" or not self.redirect_uris:
            raise ValueError("invalid public OAuth client")
        for uri in self.redirect_uris:
            parsed = urlsplit(uri)
            loopback = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"} and parsed.port is not None
            if parsed.fragment or not parsed.netloc or (parsed.scheme != "https" and not loopback):
                raise ValueError("redirect URI must be HTTPS or an explicit-port HTTP loopback URI")


@dataclass(frozen=True)
class OAuthSettings:
    database_path: Path
    issuer: str
    firebase_project_id: str
    clients: tuple[OAuthClient, ...]
    access_lifetime: int = 900
    grant_lifetime: int = 30 * 24 * 60 * 60
    firebase_web_config: dict[str, str] = field(default_factory=dict)

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

    def __post_init__(self) -> None:
        loopback = self.bind_host in {"127.0.0.1", "::1", "localhost"}
        is_http = self.oauth.canonical_issuer.startswith("http://")
        if is_http and not (self.local_development and loopback):
            raise ValueError("HTTP public origins require the loopback development profile")
        if not is_http and self.local_development:
            raise ValueError("the development profile requires a loopback HTTP origin")
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if self.oauth.database_path.parent != self.state_root:
            raise ValueError("OAuth state must be stored directly in state_root")


def load_management_settings(path: Path) -> ManagementSettings:
    """Load management YAML without accepting request-derived authority values."""

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("management configuration must be a mapping")
    state_root = Path(str(raw["state_root"])).expanduser().resolve()
    clients_raw = raw["clients"]
    if not isinstance(clients_raw, list):
        raise ValueError("clients must be a list")
    clients = tuple(OAuthClient(str(item["client_id"]), tuple(str(uri) for uri in item["redirect_uris"])) for item in clients_raw)
    oauth = OAuthSettings(
        state_root / "oauth.sqlite3",
        str(raw["public_origin"]),
        str(raw["firebase_project_id"]),
        clients,
        firebase_web_config={str(k): str(v) for k, v in dict(raw.get("firebase_web_config", {})).items()},
    )
    return ManagementSettings(
        state_root,
        oauth,
        str(raw.get("bind_host", "127.0.0.1")),
        int(raw.get("port", 8081)),
        bool(raw.get("local_development", False)),
        tuple(str(origin) for origin in raw.get("allowed_browser_origins", [])),
    )
