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
_boundary_lock = threading.Lock()
_admission_hook: AdmissionHook | None = None
_observation_hook: ObservationHook | None = None


def configure_github_request_boundary(
    admission_hook: AdmissionHook | None = None,
    observation_hook: ObservationHook | None = None,
) -> None:
    """Install the process-wide controller boundary used by every GitHub adapter."""
    global _admission_hook, _observation_hook
    with _boundary_lock:
        _admission_hook = admission_hook
        _observation_hook = observation_hook


def boundary_hooks() -> tuple[AdmissionHook | None, ObservationHook | None]:
    """Return one consistent snapshot of the configured hooks."""
    with _boundary_lock:
        return _admission_hook, _observation_hook


def _wire_outcomes() -> list[GitHubRequestOutcome]:
    """Return this thread's pending-observation list, creating it if absent.

    A plain ``getattr(_state, "wire_outcomes", [])`` returns a throwaway list
    on a fresh thread: appending to it is silently discarded because nothing
    ever assigns it back onto ``_state``. Every mutator must go through this
    helper so the list a wire attempt appends to is the same list a later
    response hook reads from, with no caller-side setup required first.
    """
    outcomes = getattr(_state, "wire_outcomes", None)
    if outcomes is None:
        outcomes = []
        _state.wire_outcomes = outcomes
    return outcomes


def begin_operation() -> None:
    _state.wire_outcomes = []


def take_wire_outcomes() -> list[GitHubRequestOutcome]:
    outcomes = _wire_outcomes()
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
    error_types: list[str] = []
    for entry in error_entries:
        if not isinstance(entry, dict):
            continue
        error_types.append(str(entry.get("type", "")))
        extensions = entry.get("extensions")
        if isinstance(extensions, dict):
            error_types.extend(str(extensions.get(key, "")) for key in ("type", "code"))
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


def normalize_api_origin(origin: str) -> str:
    """Return a credential-free canonical scheme/host/port API origin."""
    parts = urlsplit(origin)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    default_port = (scheme == "https" and parts.port == 443) or (scheme == "http" and parts.port == 80)
    authority = host if parts.port is None or default_port else f"{host}:{parts.port}"
    return urlunsplit((scheme, authority, "", "", ""))


def _safe_endpoint(url: httpx.URL) -> tuple[str, str, str | None, str | None]:
    parts = urlsplit(str(url))
    path = parts.path
    segments = path.strip("/").split("/")
    repository = "/".join(segments[1:3]) if len(segments) >= 3 and segments[0] == "repos" else None
    item_match = re.search(r"/(?:issues|pulls)/(\d+)(?:/|$)", path)
    template = re.sub(r"(?<=/)(\d+)(?=/|$)", "{id}", path)
    return normalize_api_origin(str(url)), template, repository, item_match.group(1) if item_match else None


def _sensitive_endpoint(path: str) -> bool:
    lowered = path.lower()
    return lowered in ("/app", "/user") or any(value in lowered for value in ("/actions/secrets", "/app/installations", "/access_tokens"))


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
    logger.bind(github_request=payload).debug("github_request_diagnostic {}", json.dumps(payload, default=str, sort_keys=True))


class DiagnosticTransport(httpx.BaseTransport):
    """Instrument each actual send below hishel's caching controller."""

    def __init__(self, transport: httpx.BaseTransport | None = None, admission_hook: AdmissionHook | None = None, observation_hook: ObservationHook | None = None, subsystem: str = "ghapi", api_origin: str = "https://api.github.com") -> None:
        self._transport = transport or httpx.HTTPTransport()
        self._admission_hook = admission_hook
        self._observation_hook = observation_hook
        self._subsystem = subsystem
        self._api_origin = normalize_api_origin(api_origin)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        origin, endpoint, repository, item = _safe_endpoint(request.url)
        # Redirected artifact bytes are not GitHub API traffic.  httpx removes
        # Authorization on cross-origin redirects; bypassing here also prevents
        # signed URLs and foreign quota-looking headers entering diagnostics.
        if origin != self._api_origin:
            return self._transport.handle_request(request)
        strict_read = "strict" in self._subsystem and request.method in ("GET", "HEAD")
        method = request.method.upper()
        kind = "read" if method in ("GET", "HEAD", "OPTIONS") else "mutation"
        if request.url.path.rstrip("/").endswith("/graphql") and method == "POST":
            # Classification is entirely local and the document is never logged.
            try:
                payload = json.loads(request.content)
                document = payload.get("query", "") if isinstance(payload, dict) else ""
                stripped = re.sub(r"(?s)^\s*(?:#[^\n]*\n\s*)*", "", document)
                kind = "read" if re.match(r"(?i)^(?:query\b|\{)", stripped) else "mutation"
            except (TypeError, ValueError, UnicodeDecodeError):
                kind = "mutation"
        context = GitHubRequestContext(str(request.extensions.get("auto_coder_operation_id", uuid.uuid4())), str(uuid.uuid4()), self._subsystem, origin, method, kind, endpoint, repository, item, "bypass" if strict_read else "normal", strict_read)
        header_credentials = tuple(value for key, value in request.headers.items() if key.lower() in ("authorization", "cookie"))
        credentials = header_credentials + tuple(part for value in header_credentials for part in value.split() if len(part) >= 4)
        if self._admission_hook is not None and self._admission_hook(context) is False:
            outcome = GitHubRequestOutcome(context, None, GitHubApiOutcome.REFUSED, RequestProvenance.NETWORK, DeliveryCertainty.DEFINITELY_NOT_SENT, GitHubResponseMetadata(), 0.0)
            log_outcome(outcome, "refused")
            if self._observation_hook:
                self._observation_hook(outcome)
            raise GitHubRequestRefused(outcome)
        started = time.monotonic()
        logger.bind(github_request=asdict(context)).debug("github_request_diagnostic wire_attempt")
        try:
            response = self._transport.handle_request(request)
        except GitHubRequestError:
            raise
        except Exception as exc:
            outcome = GitHubRequestOutcome(context, None, GitHubApiOutcome.TRANSPORT_FAILURE, RequestProvenance.NETWORK, DeliveryCertainty.INDETERMINATE, GitHubResponseMetadata(), (time.monotonic() - started) * 1000, _redact_message(str(exc), credentials))
            log_outcome(outcome, "transport_failure")
            # No httpx response is returned on this path, so no "response" event
            # hook will ever run to pop this outcome off the pending-observation
            # list. Deliver it to the governor directly instead of queuing it,
            # so a later, unrelated response never mistakenly pops it as its own.
            if self._observation_hook:
                self._observation_hook(outcome)
            raise GitHubRequestError(outcome) from exc
        metadata = response_metadata(response.headers)
        outcome = GitHubRequestOutcome(context, response.status_code, classify_response(response.status_code, metadata), RequestProvenance.NETWORK, DeliveryCertainty.HTTP_RESPONSE_RECEIVED, metadata, (time.monotonic() - started) * 1000)
        _wire_outcomes().append(outcome)
        return response

    def close(self) -> None:
        self._transport.close()


def github_http_client(
    *,
    subsystem: str,
    api_origin: str = "https://api.github.com",
    timeout: float | httpx.Timeout = 30.0,
    follow_redirects: bool = False,
    transport: httpx.BaseTransport | None = None,
    admission_hook: AdmissionHook | None = None,
    observation_hook: ObservationHook | None = None,
) -> httpx.Client:
    """Build an uncached client whose GitHub-origin sends share one boundary."""
    configured_admission, configured_observation = boundary_hooks()
    admission = admission_hook if admission_hook is not None else configured_admission
    observation = observation_hook if observation_hook is not None else configured_observation

    def observe(response: httpx.Response) -> None:
        outcomes = _wire_outcomes()
        if not outcomes:
            return
        base = outcomes.pop()
        try:
            response.read()
        except Exception as exc:
            finalize_read_failure(response, base, observation, exc)
            raise
        finalize_response(response, base, observation)

    return httpx.Client(
        transport=DiagnosticTransport(transport, admission, observation, subsystem, api_origin),
        timeout=timeout,
        follow_redirects=follow_redirects,
        event_hooks={"response": [observe]},
    )


def instrument_github_client(
    client: httpx.Client,
    *,
    subsystem: str,
    api_origin: str = "https://api.github.com",
) -> httpx.Client:
    """Instrument an already-created client while retaining its wire adapter."""
    transport = getattr(client, "_transport", None)
    if transport is None or not hasattr(transport, "handle_request"):
        return client
    admission, observation = boundary_hooks()
    client._transport = DiagnosticTransport(transport, admission, observation, subsystem, api_origin)  # type: ignore[attr-defined]
    # httpx may install environment-proxy transports as URL mounts.  They are
    # equally real wire routes and therefore must not bypass admission.
    for pattern, mounted in list(getattr(client, "_mounts", {}).items()):
        if mounted is not None and hasattr(mounted, "handle_request"):
            client._mounts[pattern] = DiagnosticTransport(mounted, admission, observation, subsystem, api_origin)  # type: ignore[attr-defined]

    def observe(response: httpx.Response) -> None:
        outcomes = _wire_outcomes()
        if not outcomes:
            return
        base = outcomes.pop()
        try:
            response.read()
        except Exception as exc:
            finalize_read_failure(response, base, observation, exc)
            raise
        finalize_response(response, base, observation)

    client.event_hooks["response"].append(observe)
    return client


def finalize_read_failure(
    response: httpx.Response,
    base: GitHubRequestOutcome,
    observation_hook: ObservationHook | None,
    exc: Exception,
) -> GitHubRequestOutcome:
    """Deliver a terminal observation when the body could not be read.

    The status line and headers were already received over the wire, so
    delivery is genuinely HTTP_RESPONSE_RECEIVED, not fabricated as
    definitely-not-sent; classification and any throttle metadata come from
    the headers alone, since the body (and any structured error it carries)
    is unavailable. This must run for every exit out of the response hook
    that isn't a normal decoded response, so a completed local attempt is
    never left indefinitely classified as in-flight in the governor.
    """
    header_credentials = tuple(value for key, value in response.request.headers.items() if key.lower() in ("authorization", "cookie"))
    credentials = header_credentials + tuple(part for value in header_credentials for part in value.split() if len(part) >= 4)
    metadata = response_metadata(response.headers)
    outcome = GitHubRequestOutcome(
        base.context,
        response.status_code,
        classify_response(response.status_code, metadata),
        base.provenance,
        DeliveryCertainty.HTTP_RESPONSE_RECEIVED,
        metadata,
        base.elapsed_ms,
        _redact_message(str(exc), credentials),
    )
    log_outcome(outcome, "body_read_failure")
    if observation_hook:
        observation_hook(outcome)
    return outcome


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
