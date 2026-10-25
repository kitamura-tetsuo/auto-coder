"""Configuration for the management OAuth credential authority."""

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

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
