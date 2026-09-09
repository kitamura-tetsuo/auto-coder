from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from unittest.mock import MagicMock

import httpx
import pytest

from auto_coder.automation_engine import AutomationEngine
from auto_coder.github_request_governor import GitHubRequestDeferred, GitHubRequestGovernor
from auto_coder.util.gh_cache import GitHubClient, get_ghapi_client
from auto_coder.util.github_request_outcome import (
    DeliveryCertainty,
    DiagnosticTransport,
    GitHubApiOutcome,
    GitHubRequestContext,
    GitHubRequestError,
    GitHubRequestOutcome,
    GitHubResponseMetadata,
    RequestProvenance,
    configure_github_request_boundary,
    github_http_client,
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


def test_rolling_attempt_and_mutation_budgets_use_attempt_times(tmp_path) -> None:
    clock = Clock()
    governor = GitHubRequestGovernor(monotonic=clock.monotonic, wall_time=clock.wall, store_path=tmp_path / "requests.sqlite3")
    for attempt in range(300):
        admit_and_finish(governor, context(attempt))

    governor = GitHubRequestGovernor(monotonic=clock.monotonic, wall_time=clock.wall, store_path=tmp_path / "requests.sqlite3")
    with pytest.raises(GitHubRequestDeferred) as request_limit:
        governor.admit(context(301))
    assert request_limit.value.reason == "request_rolling_window"
    assert request_limit.value.retry_at == clock.wall_value + 60

    clock.advance(60)
    mutation_governor = GitHubRequestGovernor(monotonic=clock.monotonic, wall_time=clock.wall, store_path=tmp_path / "minute.sqlite3")
    for attempt in range(60):
        admit_and_finish(mutation_governor, context(attempt, "mutation"))
        clock.advance(1)
    # One-second completion spacing is the stronger bound: when the sixty-first
    # attempt is eligible, the first attempt has just left the rolling window.
    admit_and_finish(mutation_governor, context(61, "mutation"))

    hour_governor = GitHubRequestGovernor(monotonic=clock.monotonic, wall_time=clock.wall, store_path=tmp_path / "hour.sqlite3")
    for attempt in range(400):
        admit_and_finish(hour_governor, context(attempt, "mutation"))
        clock.advance(8)
    hour_governor = GitHubRequestGovernor(monotonic=clock.monotonic, wall_time=clock.wall, store_path=tmp_path / "hour.sqlite3")
    with pytest.raises(GitHubRequestDeferred) as hour_limit:
        hour_governor.admit(context(401, "mutation"))
    assert hour_limit.value.reason == "mutation_hour_window"


def test_concurrency_and_mutation_completion_spacing_are_atomic(tmp_path) -> None:
    clock = Clock()
    path = tmp_path / "spacing.sqlite3"
    governor = GitHubRequestGovernor(monotonic=clock.monotonic, wall_time=clock.wall, store_path=path)
    first = context(1, "mutation")
    governor.admit(first)
    with pytest.raises(GitHubRequestDeferred) as concurrent:
        governor.admit(context(2))
    assert concurrent.value.reason == "request_in_flight"

    clock.advance(5)
    governor.observe(outcome(first, GitHubApiOutcome.TRANSPORT_FAILURE))
    governor = GitHubRequestGovernor(monotonic=clock.monotonic, wall_time=clock.wall, store_path=path)
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


def test_fresh_thread_uncached_completion_resolves_its_reservation(tmp_path) -> None:
    """REQ-001/AS-001: a brand-new thread's first request must not lose its
    completion observation. `getattr(_state, "wire_outcomes", [])` used to
    hand back a throwaway list on a thread that never called
    `begin_operation()`; appending to it silently discarded the outcome and
    left the governor reservation resolved=0 forever."""
    governor = GitHubRequestGovernor(store_path=tmp_path / "fresh_thread.sqlite3")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True}, request=request)

    errors: list[BaseException] = []

    def worker() -> None:
        try:
            with github_http_client(
                subsystem="controller-strict",
                transport=httpx.MockTransport(handler),
                admission_hook=governor.admit,
                observation_hook=governor.observe,
            ) as client:
                client.get("https://api.github.com/repos/acme/widgets")
        except BaseException as exc:  # pragma: no cover - surfaced via assertion below
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=2)
    assert errors == []

    # The completed attempt must be resolved: an unrelated admission on the
    # same origin must not be refused as still in-flight.
    assert governor.admit(context(2)) is True


def test_direct_caching_client_usage_resolves_its_reservation(tmp_path, monkeypatch) -> None:
    """REQ-001/AS-001: a caller that talks to get_caching_client() directly
    (bypassing the GhApi wrapper -- e.g. GitHubClient.get_open_pull_requests)
    must resolve its Governor reservation on success too. Before this fix,
    get_caching_client()'s SyncCacheClient had no completion hook for callers
    that invoke client.request() themselves, so the reservation stayed
    unresolved and every later same-origin request was refused as
    request_in_flight for the rest of the process's life.

    This goes through the real get_caching_client() factory and a real,
    durably-stored GitHubRequestGovernor; only the actual socket send
    (httpx.HTTPTransport.handle_request) is replaced, matching AS-001's
    requirement that the boundary itself -- not a stand-in for it -- is what
    gets exercised.
    """
    governor = GitHubRequestGovernor(store_path=tmp_path / "direct_usage.sqlite3")
    configure_github_request_boundary(governor.admit, governor.observe)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"number": 1}], request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda self, request: handler(request))
    try:
        result = GitHubClient("token").get_open_pull_requests("acme/widgets")
        assert result == [{"number": 1}]

        # The completed attempt must be resolved: a later admission on the same
        # origin must not be refused as still in-flight.
        assert governor.admit(context(2)) is True
    finally:
        configure_github_request_boundary()


def test_production_transport_cooldown_and_episode_survive_fresh_instance(tmp_path, monkeypatch) -> None:
    """AS-002: the real transport cannot send during a recovered cooldown."""
    clock = Clock()
    path = tmp_path / "request_governor.sqlite3"
    sends: list[str] = []

    def throttled(request: httpx.Request) -> httpx.Response:
        sends.append(request.url.path)
        return httpx.Response(429, headers={"Retry-After": "600"}, json={"message": "rate limited"}, request=request)

    first = GitHubRequestGovernor(monotonic=clock.monotonic, wall_time=clock.wall, store_path=path)
    configure_github_request_boundary(first.admit, first.observe)
    monkeypatch.setattr("auto_coder.util.gh_cache.get_caching_client", lambda *args, **kwargs: httpx.Client(transport=DiagnosticTransport(httpx.MockTransport(throttled), admission_hook=first.admit, observation_hook=first.observe)))
    with pytest.raises(GitHubRequestError):
        get_ghapi_client("recognizable-secret")("/user")
    assert sends == ["/user"]

    clock.advance(30)
    restarted = GitHubRequestGovernor(monotonic=clock.monotonic, wall_time=clock.wall, store_path=path)
    configure_github_request_boundary(restarted.admit, restarted.observe)
    monkeypatch.setattr("auto_coder.util.gh_cache.get_caching_client", lambda *args, **kwargs: httpx.Client(transport=DiagnosticTransport(httpx.MockTransport(throttled), admission_hook=restarted.admit, observation_hook=restarted.observe)))
    with pytest.raises(GitHubRequestDeferred) as deferred:
        get_ghapi_client("rotated-secret")("/app/installations")
    assert deferred.value.retry_at == clock.wall_value + 570
    assert sends == ["/user"]
    assert b"recognizable-secret" not in path.read_bytes()


def test_unresolved_reservation_recovery_is_charged_and_not_replayed(tmp_path) -> None:
    """AS-003: a crash marker adds cooldown without producing network activity."""
    clock = Clock()
    path = tmp_path / "request_governor.sqlite3"
    first = GitHubRequestGovernor(monotonic=clock.monotonic, wall_time=clock.wall, store_path=path)
    assert first.admit(context(1, "mutation")) is True

    restarted = GitHubRequestGovernor(monotonic=clock.monotonic, wall_time=clock.wall, store_path=path)
    with pytest.raises(GitHubRequestDeferred) as recovered:
        restarted.admit(context(2))
    assert recovered.value.reason == "rate_limit_cooldown"
    assert recovered.value.retry_at == clock.wall_value + 60
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT resolved, recovered FROM reservations WHERE attempt_id='attempt-1'").fetchone() == (0, 1)


def test_corrupt_and_invalid_timing_state_fail_network_admission_closed(tmp_path) -> None:
    """AS-004/005: required unreadable or nonfinite state is never replaced."""
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not a sqlite database")
    unavailable = GitHubRequestGovernor(store_path=corrupt)
    with pytest.raises(GitHubRequestDeferred) as refused:
        unavailable.admit(context(1))
    assert refused.value.reason == "governor_state_unavailable"
    assert corrupt.read_bytes() == b"not a sqlite database"

    clock = Clock()
    invalid = tmp_path / "invalid.sqlite3"
    valid = GitHubRequestGovernor(monotonic=clock.monotonic, wall_time=clock.wall, store_path=invalid)
    valid.admit(context(2))
    valid.observe(outcome(context(2)))
    with sqlite3.connect(invalid) as connection:
        connection.execute("UPDATE governor_metadata SET last_logical_utc=?", (float("inf"),))
    reopened = GitHubRequestGovernor(monotonic=clock.monotonic, wall_time=clock.wall, store_path=invalid)
    with pytest.raises(GitHubRequestDeferred) as invalid_refusal:
        reopened.admit(context(3))
    assert invalid_refusal.value.reason == "governor_state_unavailable"


def test_blocking_admission_waits_out_a_concurrent_attempt(tmp_path, monkeypatch) -> None:
    """The controller's own pacing must delay a concurrent request, not fail it.

    `admit` refuses a second attempt while one is in flight. With that refusal
    installed as the process-wide boundary, every concurrent worker read failed
    as `GitHub request failed: refused`. `admit_blocking` keeps the same
    single-flight policy but makes the second caller wait for its turn, so both
    requests are actually sent.
    """
    governor = GitHubRequestGovernor(store_path=tmp_path / "waiting.sqlite3")
    entered = threading.Event()
    release = threading.Event()
    sends: list[str] = []
    sends_lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        with sends_lock:
            sends.append(request.url.path)
        entered.set()
        release.wait(timeout=5)
        return httpx.Response(200, json={"ok": True}, request=request)

    monkeypatch.setattr(
        "auto_coder.util.gh_cache.get_caching_client",
        lambda *args, **kwargs: httpx.Client(
            transport=DiagnosticTransport(
                httpx.MockTransport(handler),
                admission_hook=governor.admit_blocking,
                observation_hook=governor.observe,
            )
        ),
    )
    configure_github_request_boundary(governor.admit_blocking, governor.observe)
    errors: list[BaseException] = []

    def request(path: str) -> None:
        try:
            get_ghapi_client("token")(path)
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=request, args=("/repos/acme/one",))
    first.start()
    try:
        assert entered.wait(timeout=5)
        second = threading.Thread(target=request, args=("/repos/acme/two",))
        second.start()
        # The second attempt is held at admission, not sent alongside the first.
        second.join(timeout=0.5)
        assert second.is_alive()
        with sends_lock:
            assert sends == ["/repos/acme/one"]
        release.set()
        second.join(timeout=5)
        assert not second.is_alive()
    finally:
        release.set()
        first.join(timeout=5)
        configure_github_request_boundary()
    assert errors == []
    assert sends == ["/repos/acme/one", "/repos/acme/two"]
    # Both reservations resolved, so an unrelated admission is still eligible.
    assert governor.admit(context(3)) is True


def test_blocking_admission_gives_up_on_a_stuck_attempt(tmp_path) -> None:
    """Waiting is bounded: an attempt that never completes still defers."""
    clock = Clock()
    waits: list[float] = []

    def waiter(seconds: float) -> None:
        waits.append(seconds)
        clock.advance(seconds)

    governor = GitHubRequestGovernor(
        monotonic=clock.monotonic,
        wall_time=clock.wall,
        store_path=tmp_path / "stuck.sqlite3",
        wait_budget=2.0,
        waiter=waiter,
    )
    assert governor.admit(context(1)) is True

    with pytest.raises(GitHubRequestDeferred) as exhausted:
        governor.admit_blocking(context(2))
    assert exhausted.value.reason == "request_in_flight"
    assert sum(waits) == pytest.approx(2.0)
    assert waits and max(waits) <= 0.5


def test_blocking_admission_does_not_wait_out_real_backpressure(tmp_path) -> None:
    """Throttle cooldown and unusable state stay immediate, durable deferrals."""
    clock = Clock()
    waits: list[float] = []
    path = tmp_path / "cooldown.sqlite3"
    governor = GitHubRequestGovernor(monotonic=clock.monotonic, wall_time=clock.wall, store_path=path, waiter=waits.append)
    first = context(1)
    governor.admit(first)
    governor.observe(outcome(first, GitHubApiOutcome.SECONDARY_THROTTLED, GitHubResponseMetadata(retry_after_seconds=600)))

    with pytest.raises(GitHubRequestDeferred) as throttled:
        governor.admit_blocking(context(2))
    assert throttled.value.reason == "rate_limit_cooldown"
    assert throttled.value.retry_at == clock.wall_value + 600
    assert waits == []

    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not a sqlite database")
    unavailable = GitHubRequestGovernor(store_path=corrupt, waiter=waits.append)
    with pytest.raises(GitHubRequestDeferred) as refused:
        unavailable.admit_blocking(context(3))
    assert refused.value.reason == "governor_state_unavailable"
    assert waits == []


def test_blocking_admission_waits_out_mutation_spacing(tmp_path) -> None:
    """Self-imposed spacing delays the mutation and then admits it."""
    clock = Clock()
    waits: list[float] = []

    def waiter(seconds: float) -> None:
        waits.append(seconds)
        clock.advance(seconds)

    governor = GitHubRequestGovernor(monotonic=clock.monotonic, wall_time=clock.wall, store_path=tmp_path / "spacing.sqlite3", waiter=waiter)
    first = context(1, "mutation")
    governor.admit(first)
    governor.observe(outcome(first))

    assert governor.admit_blocking(context(2, "mutation")) is True
    assert sum(waits) == pytest.approx(1.0)
