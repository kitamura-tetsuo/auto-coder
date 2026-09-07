"""Typed, secret-safe observations for traffic at the GitHub HTTP boundary."""

from __future__ import annotations

import json
import math
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import cast
from urllib.parse import urlsplit, urlunsplit

import httpx

from ..logger_config import get_logger

logger = get_logger(__name__)


class GitHubApiOutcome(str, Enum):
    SUCCESS = "success"
    AUTHENTICATION_FAILURE = "authentication_failure"
    PRIMARY_THROTTLED = "primary_throttled"
    SECONDARY_THROTTLED = "secondary_throttled"
    THROTTLED = "throttled"
    FORBIDDEN = "forbidden_unclassified"
    REMOTE_ERROR = "remote_error"
    TRANSPORT_FAILURE = "transport_failure"
    REFUSED = "refused"


class DeliveryCertainty(str, Enum):
    DEFINITELY_NOT_SENT = "definitely_not_sent"
    HTTP_RESPONSE_RECEIVED = "http_response_received"
    INDETERMINATE = "indeterminate_after_possible_send"


class RequestProvenance(str, Enum):
    NETWORK = "network"
    LOCAL_CACHE = "local_cache"


@dataclass(frozen=True)
class GitHubResponseMetadata:
    retry_after_seconds: float | None = None
    rate_limit_limit: int | None = None
    rate_limit_remaining: int | None = None
    rate_limit_used: int | None = None
    rate_limit_reset: float | None = None
    rate_limit_resource: str | None = None
    github_request_id: str | None = None


@dataclass(frozen=True)
class GitHubRequestContext:
    operation_id: str
    attempt_id: str
    subsystem: str
    api_origin: str
    method: str
    kind: str
    endpoint_template: str
    repository: str | None = None
    item: str | None = None
    cache_mode: str = "normal"
    strict_read: bool = False


@dataclass(frozen=True)
class GitHubRequestOutcome:
    context: GitHubRequestContext
    status: int | None
    classification: GitHubApiOutcome
    provenance: RequestProvenance
    delivery: DeliveryCertainty
    metadata: GitHubResponseMetadata
    elapsed_ms: float
    message: str | None = None
    documentation_url: str | None = None


class GitHubRequestError(RuntimeError):
    """A GitHub failure retaining its typed outcome without exposing payloads."""

    def __init__(self, outcome: GitHubRequestOutcome) -> None:
        self.outcome = outcome
        self.status_code = outcome.status
        super().__init__(f"GitHub request failed: {outcome.classification.value}")


class GitHubRequestRefused(GitHubRequestError):
    """An admission decision made before the transport sent any bytes."""


AdmissionHook = Callable[[GitHubRequestContext], bool | None]
ObservationHook = Callable[[GitHubRequestOutcome], None]

_state = threading.local()


def begin_operation() -> None:
    _state.wire_outcomes = []


def take_wire_outcomes() -> list[GitHubRequestOutcome]:
    outcomes = getattr(_state, "wire_outcomes", [])
    _state.wire_outcomes = []
    return list(outcomes)


def _optional_nonnegative_number(value: str | None, integer: bool = False) -> float | int | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0 or (integer and not number.is_integer()):
        return None
    return int(number) if integer else number


def _retry_after(value: str | None, now: datetime | None = None) -> float | None:
    numeric = _optional_nonnegative_number(value)
    if numeric is not None:
        return float(numeric)
    if value is None:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        delay = (parsed.astimezone(timezone.utc) - (now or datetime.now(timezone.utc))).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return None
    return delay if math.isfinite(delay) and delay >= 0 else None


def response_metadata(headers: Mapping[str, str]) -> GitHubResponseMetadata:
    try:
        lowered = {key.lower(): value for key, value in headers.items()}
    except TypeError:
        # A response adapter without a concrete mapping has no trustworthy
        # metadata; unavailable is deliberately different from zero.
        lowered = {}
    return GitHubResponseMetadata(
        retry_after_seconds=_retry_after(lowered.get("retry-after")),
        rate_limit_limit=cast(int | None, _optional_nonnegative_number(lowered.get("x-ratelimit-limit"), True)),
        rate_limit_remaining=cast(int | None, _optional_nonnegative_number(lowered.get("x-ratelimit-remaining"), True)),
        rate_limit_used=cast(int | None, _optional_nonnegative_number(lowered.get("x-ratelimit-used"), True)),
        rate_limit_reset=_optional_nonnegative_number(lowered.get("x-ratelimit-reset")),
        rate_limit_resource=lowered.get("x-ratelimit-resource"),
        github_request_id=lowered.get("x-github-request-id"),
    )


_SECONDARY = re.compile(r"secondary rate limit|abuse detection", re.IGNORECASE)


def classify_response(status: int, metadata: GitHubResponseMetadata, errors: object = None, message: str = "") -> GitHubApiOutcome:
    error_entries = errors if isinstance(errors, list) else []
    error_messages = [str(entry.get("message", "")) for entry in error_entries if isinstance(entry, dict)]
    error_types = [str(entry.get("type", "")) for entry in error_entries if isinstance(entry, dict)]
    combined = " ".join([message, *error_messages])
    primary = metadata.rate_limit_remaining == 0
    secondary = metadata.retry_after_seconds is not None or bool(_SECONDARY.search(combined))
    graphql_throttle = any(value.upper() == "RATE_LIMITED" for value in error_types) or secondary or (bool(error_entries) and primary)
    if status == 401:
        return GitHubApiOutcome.AUTHENTICATION_FAILURE
    if status == 403:
        if primary:
            return GitHubApiOutcome.PRIMARY_THROTTLED
        if secondary:
            return GitHubApiOutcome.SECONDARY_THROTTLED
        return GitHubApiOutcome.FORBIDDEN
    if status == 429:
        if primary:
            return GitHubApiOutcome.PRIMARY_THROTTLED
        if secondary:
            return GitHubApiOutcome.SECONDARY_THROTTLED
        return GitHubApiOutcome.THROTTLED
    if error_entries:
        if graphql_throttle:
            return GitHubApiOutcome.PRIMARY_THROTTLED if primary else GitHubApiOutcome.SECONDARY_THROTTLED if secondary else GitHubApiOutcome.THROTTLED
        return GitHubApiOutcome.REMOTE_ERROR
    return GitHubApiOutcome.SUCCESS if status < 400 else GitHubApiOutcome.REMOTE_ERROR


def _safe_endpoint(url: httpx.URL) -> tuple[str, str, str | None, str | None]:
    parts = urlsplit(str(url))
    path = parts.path
    segments = path.strip("/").split("/")
    repository = "/".join(segments[1:3]) if len(segments) >= 3 and segments[0] == "repos" else None
    item_match = re.search(r"/(?:issues|pulls)/(\d+)(?:/|$)", path)
    template = re.sub(r"(?<=/)(\d+)(?=/|$)", "{id}", path)
    return urlunsplit((parts.scheme, parts.netloc, "", "", "")), template, repository, item_match.group(1) if item_match else None


def _sensitive_endpoint(path: str) -> bool:
    lowered = path.lower()
    return any(value in lowered for value in ("/actions/secrets", "/app/installations", "/access_tokens"))


def _redact_message(message: str, credentials: tuple[str, ...]) -> str:
    result = message
    for credential in credentials:
        if credential:
            result = result.replace(credential, "[REDACTED]")
    result = re.sub(r"(?i)(authorization|cookie|token|secret|private[_ -]?key)\s*[:=]\s*\S+", r"\1=[REDACTED]", result)
    result = re.sub(r"https?://[^\s?]+\?\S+", "[REDACTED_URL]", result)
    return result[:1024]


def safe_error_details(response: httpx.Response, credentials: tuple[str, ...]) -> tuple[str | None, str | None]:
    if _sensitive_endpoint(response.request.url.path):
        return None, None
    try:
        body = response.json()
    except (ValueError, json.JSONDecodeError):
        return None, None
    if not isinstance(body, dict):
        return None, None
    message = body.get("message")
    documentation = body.get("documentation_url")
    safe_message = _redact_message(str(message), credentials) if isinstance(message, str) else None
    safe_documentation = None
    if isinstance(documentation, str):
        parts = urlsplit(documentation)
        safe_documentation = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return safe_message, safe_documentation


def log_outcome(outcome: GitHubRequestOutcome, event: str) -> None:
    payload = asdict(outcome)
    payload["event"] = event
    logger.bind(github_request=payload).info("github_request_diagnostic {}", json.dumps(payload, default=str, sort_keys=True))


class DiagnosticTransport(httpx.BaseTransport):
    """Instrument each actual send below hishel's caching controller."""

    def __init__(self, transport: httpx.BaseTransport | None = None, admission_hook: AdmissionHook | None = None, observation_hook: ObservationHook | None = None, subsystem: str = "ghapi") -> None:
        self._transport = transport or httpx.HTTPTransport()
        self._admission_hook = admission_hook
        self._observation_hook = observation_hook
        self._subsystem = subsystem

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        origin, endpoint, repository, item = _safe_endpoint(request.url)
        context = GitHubRequestContext(str(request.extensions.get("auto_coder_operation_id", uuid.uuid4())), str(uuid.uuid4()), self._subsystem, origin, request.method, "read" if request.method in ("GET", "HEAD") else "mutation", endpoint, repository, item)
        header_credentials = tuple(value for key, value in request.headers.items() if key.lower() in ("authorization", "cookie"))
        credentials = header_credentials + tuple(part for value in header_credentials for part in value.split() if len(part) >= 4)
        if self._admission_hook is not None and self._admission_hook(context) is False:
            outcome = GitHubRequestOutcome(context, None, GitHubApiOutcome.REFUSED, RequestProvenance.NETWORK, DeliveryCertainty.DEFINITELY_NOT_SENT, GitHubResponseMetadata(), 0.0)
            log_outcome(outcome, "refused")
            if self._observation_hook:
                self._observation_hook(outcome)
            raise GitHubRequestRefused(outcome)
        started = time.monotonic()
        logger.bind(github_request=asdict(context)).info("github_request_diagnostic wire_attempt")
        try:
            response = self._transport.handle_request(request)
        except GitHubRequestError:
            raise
        except Exception as exc:
            outcome = GitHubRequestOutcome(context, None, GitHubApiOutcome.TRANSPORT_FAILURE, RequestProvenance.NETWORK, DeliveryCertainty.INDETERMINATE, GitHubResponseMetadata(), (time.monotonic() - started) * 1000, _redact_message(str(exc), credentials))
            log_outcome(outcome, "transport_failure")
            getattr(_state, "wire_outcomes", []).append(outcome)
            if self._observation_hook:
                self._observation_hook(outcome)
            raise GitHubRequestError(outcome) from exc
        metadata = response_metadata(response.headers)
        outcome = GitHubRequestOutcome(context, response.status_code, classify_response(response.status_code, metadata), RequestProvenance.NETWORK, DeliveryCertainty.HTTP_RESPONSE_RECEIVED, metadata, (time.monotonic() - started) * 1000)
        getattr(_state, "wire_outcomes", []).append(outcome)
        return response

    def close(self) -> None:
        self._transport.close()


def finalize_response(
    response: httpx.Response,
    base: GitHubRequestOutcome,
    observation_hook: ObservationHook | None = None,
    event: str = "response",
) -> GitHubRequestOutcome:
    body: object = None
    try:
        body = response.json()
    except Exception:
        # Decoding is diagnostic-only and must never alter successful payload
        # delivery. No exception text is logged from this path.
        pass
    errors = body.get("errors") if isinstance(body, dict) else None
    raw_message = body.get("message", "") if isinstance(body, dict) else ""
    header_credentials = tuple(value for key, value in response.request.headers.items() if key.lower() in ("authorization", "cookie")) if isinstance(response, httpx.Response) else ()
    credentials = header_credentials + tuple(part for value in header_credentials for part in value.split() if len(part) >= 4)
    message, documentation = safe_error_details(response, credentials) if response.status_code >= 400 or errors else (None, None)
    outcome = GitHubRequestOutcome(base.context, response.status_code, classify_response(response.status_code, base.metadata, errors, str(raw_message)), base.provenance, base.delivery, base.metadata, base.elapsed_ms, message, documentation)
    log_outcome(outcome, event)
    if observation_hook:
        observation_hook(outcome)
    return outcome
