from __future__ import annotations

import threading
from dataclasses import dataclass
from unittest.mock import MagicMock

import httpx
import pytest

from auto_coder.automation_engine import AutomationEngine
from auto_coder.github_request_governor import GitHubRequestDeferred, GitHubRequestGovernor
from auto_coder.util.gh_cache import get_ghapi_client
from auto_coder.util.github_request_outcome import (
    DeliveryCertainty,
    DiagnosticTransport,
    GitHubApiOutcome,
    GitHubRequestContext,
    GitHubRequestOutcome,
    GitHubResponseMetadata,
    RequestProvenance,
    configure_github_request_boundary,
)


@dataclass
class Clock:
    monotonic_value: float = 100.0
    wall_value: float = 1_800_000_000.0

    def monotonic(self) -> float:
        return self.monotonic_value

    def wall(self) -> float:
        return self.wall_value

    def advance(self, seconds: float) -> None:
        self.monotonic_value += seconds
        self.wall_value += seconds


def context(attempt: int, kind: str = "read", origin: str = "https://API.GITHUB.com:443") -> GitHubRequestContext:
    return GitHubRequestContext("operation", f"attempt-{attempt}", "test", origin, "GET" if kind == "read" else "POST", kind, "/endpoint")


def outcome(
    request_context: GitHubRequestContext,
    classification: GitHubApiOutcome = GitHubApiOutcome.SUCCESS,
    metadata: GitHubResponseMetadata = GitHubResponseMetadata(),
) -> GitHubRequestOutcome:
    return GitHubRequestOutcome(
        request_context,
        200,
        classification,
        RequestProvenance.NETWORK,
        DeliveryCertainty.HTTP_RESPONSE_RECEIVED,
        metadata,
        1.0,
    )


def admit_and_finish(governor: GitHubRequestGovernor, request_context: GitHubRequestContext) -> None:
    assert governor.admit(request_context) is True
    governor.observe(outcome(request_context))


def test_rolling_attempt_and_mutation_budgets_use_attempt_times() -> None:
    clock = Clock()
    governor = GitHubRequestGovernor(monotonic=clock.monotonic, wall_time=clock.wall)
    for attempt in range(300):
        admit_and_finish(governor, context(attempt))

    with pytest.raises(GitHubRequestDeferred) as request_limit:
        governor.admit(context(301))
    assert request_limit.value.reason == "request_rolling_window"
    assert request_limit.value.retry_at == clock.wall_value + 60

    clock.advance(60)
    mutation_governor = GitHubRequestGovernor(monotonic=clock.monotonic, wall_time=clock.wall)
    for attempt in range(60):
        admit_and_finish(mutation_governor, context(attempt, "mutation"))
        clock.advance(1)
    # One-second completion spacing is the stronger bound: when the sixty-first
    # attempt is eligible, the first attempt has just left the rolling window.
    admit_and_finish(mutation_governor, context(61, "mutation"))

    hour_governor = GitHubRequestGovernor(monotonic=clock.monotonic, wall_time=clock.wall)
    for attempt in range(400):
        admit_and_finish(hour_governor, context(attempt, "mutation"))
        clock.advance(8)
    with pytest.raises(GitHubRequestDeferred) as hour_limit:
        hour_governor.admit(context(401, "mutation"))
    assert hour_limit.value.reason == "mutation_hour_window"


def test_concurrency_and_mutation_completion_spacing_are_atomic() -> None:
    clock = Clock()
    governor = GitHubRequestGovernor(monotonic=clock.monotonic, wall_time=clock.wall)
    first = context(1, "mutation")
    governor.admit(first)
    with pytest.raises(GitHubRequestDeferred) as concurrent:
        governor.admit(context(2))
    assert concurrent.value.reason == "request_in_flight"

    clock.advance(5)
    governor.observe(outcome(first, GitHubApiOutcome.TRANSPORT_FAILURE))
    with pytest.raises(GitHubRequestDeferred) as spacing:
        governor.admit(context(3, "mutation"))
    assert spacing.value.reason == "mutation_spacing"
    clock.advance(1)
    assert governor.admit(context(4, "mutation")) is True


def test_throttle_evidence_uses_latest_deadline_and_episode_backoff() -> None:
    clock = Clock()
    governor = GitHubRequestGovernor(monotonic=clock.monotonic, wall_time=clock.wall)
    first = context(1)
    governor.admit(first)
    governor.observe(
        outcome(
            first,
            GitHubApiOutcome.SECONDARY_THROTTLED,
            GitHubResponseMetadata(retry_after_seconds=120, rate_limit_remaining=0, rate_limit_reset=clock.wall_value + 180),
        )
    )
    with pytest.raises(GitHubRequestDeferred) as deferred:
        governor.admit(context(2))
    assert deferred.value.reason == "rate_limit_cooldown"
    assert deferred.value.retry_at == clock.wall_value + 180

    clock.advance(180)
    second = context(3)
    governor.admit(second)
    governor.observe(outcome(second, GitHubApiOutcome.THROTTLED, GitHubResponseMetadata(retry_after_seconds=5000)))
    with pytest.raises(GitHubRequestDeferred) as repeated:
        governor.admit(context(4))
    assert repeated.value.retry_at == clock.wall_value + 5000


def test_remaining_zero_and_only_post_cooldown_success_end_episode() -> None:
    clock = Clock()
    governor = GitHubRequestGovernor(monotonic=clock.monotonic, wall_time=clock.wall)
    first = context(1)
    governor.admit(first)
    governor.observe(outcome(first, GitHubApiOutcome.THROTTLED))
    clock.advance(60)
    second = context(2)
    governor.admit(second)
    governor.observe(outcome(second, metadata=GitHubResponseMetadata(rate_limit_remaining=0, rate_limit_reset=clock.wall_value - 1)))
    with pytest.raises(GitHubRequestDeferred) as zero:
        governor.admit(context(3))
    assert zero.value.retry_at == clock.wall_value + 60

    clock.advance(60)
    third = context(4)
    governor.admit(third)
    governor.observe(outcome(third))
    fourth = context(5)
    governor.admit(fourth)
    governor.observe(outcome(fourth, GitHubApiOutcome.THROTTLED))
    with pytest.raises(GitHubRequestDeferred) as new_episode:
        governor.admit(context(6))
    assert new_episode.value.retry_at == clock.wall_value + 60


def test_real_ghapi_transport_shares_origins_and_classifies_graphql_locally(monkeypatch) -> None:
    starts: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        starts.append((request.method, request.url.path))
        return httpx.Response(200, json={"data": {}}, request=request)

    def client(*args, **kwargs):
        return httpx.Client(
            transport=DiagnosticTransport(
                httpx.MockTransport(handler),
                admission_hook=governor.admit,
                observation_hook=governor.observe,
            )
        )

    monkeypatch.setattr("auto_coder.util.gh_cache.get_caching_client", client)
    engine = AutomationEngine(MagicMock())
    governor = engine.github_request_governor
    try:
        get_ghapi_client("ordinary")("/graphql", verb="POST", data={"query": "query Q { viewer { login } }"})
        # A successful GraphQL query did not consume mutation spacing.
        get_ghapi_client("app-token")("/graphql", verb="POST", data={"query": "mutation M { doThing }"})
        with pytest.raises(GitHubRequestDeferred) as paced:
            get_ghapi_client("third-token")("/repos/acme/other/issues/1/comments", verb="POST", data={"body": "not logged"})
        assert paced.value.reason == "mutation_spacing"
    finally:
        configure_github_request_boundary()
    assert starts == [("POST", "/graphql"), ("POST", "/graphql")]


def test_concurrent_real_clients_never_overlap(monkeypatch) -> None:
    governor = GitHubRequestGovernor()
    entered = threading.Event()
    release = threading.Event()
    sends: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sends.append(request.url.path)
        entered.set()
        release.wait(timeout=2)
        return httpx.Response(200, json={"ok": True}, request=request)

    monkeypatch.setattr(
        "auto_coder.util.gh_cache.get_caching_client",
        lambda *args, **kwargs: httpx.Client(
            transport=DiagnosticTransport(
                httpx.MockTransport(handler),
                admission_hook=governor.admit,
                observation_hook=governor.observe,
            )
        ),
    )
    configure_github_request_boundary(governor.admit, governor.observe)
    errors: list[BaseException] = []

    def first_request() -> None:
        try:
            get_ghapi_client("one")("/repos/acme/one")
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=first_request)
    thread.start()
    assert entered.wait(timeout=2)
    try:
        with pytest.raises(GitHubRequestDeferred) as blocked:
            get_ghapi_client("two")("/repos/acme/two")
        assert blocked.value.reason == "request_in_flight"
        assert sends == ["/repos/acme/one"]
    finally:
        release.set()
        thread.join(timeout=2)
        configure_github_request_boundary()
    assert errors == []
