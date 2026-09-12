from __future__ import annotations

import fcntl
import sqlite3
import threading
from dataclasses import dataclass
from multiprocessing import get_context
from multiprocessing.connection import Connection
from pathlib import Path
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


def test_deferral_message_preserves_local_reason_and_retry_deadline() -> None:
    deferred = GitHubRequestDeferred(context(1), "governor_state_unavailable", 1800000060.0)
    assert str(deferred) == "GitHub request deferred before sending: governor_state_unavailable; retry_at=1800000060.0 (Unix seconds)"
    assert deferred.reason == "governor_state_unavailable"
    assert deferred.retry_at == 1800000060.0
    assert deferred.outcome.classification is GitHubApiOutcome.REFUSED
    assert deferred.outcome.delivery is DeliveryCertainty.DEFINITELY_NOT_SENT


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


def _receive(channel: Connection, timeout: float = 10.0) -> object:
    assert channel.poll(timeout), "timed out waiting for child process"
    message = channel.recv()
    if isinstance(message, tuple) and message and message[0] == "error":
        pytest.fail(f"child process failed: {message[1]}")
    return message


def _hold_process_reservation(path: str, ready: Connection) -> None:
    governor = GitHubRequestGovernor(store_path=Path(path))
    governor.admit(context(900))
    ready.send("admitted")
    assert ready.poll(10)
    ready.recv()


def _create_legacy_store(path: Path, timestamp: float) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE governor_metadata(singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema_version INTEGER NOT NULL, last_logical_utc REAL NOT NULL);
            CREATE TABLE origin_state(
                origin TEXT PRIMARY KEY,
                cooldown_until_utc REAL NOT NULL DEFAULT 0,
                cooldown_reason TEXT NOT NULL DEFAULT '',
                throttle_count INTEGER NOT NULL DEFAULT 0,
                episode_active INTEGER NOT NULL DEFAULT 0 CHECK(episode_active IN (0,1)),
                last_mutation_completion_utc REAL
            );
            CREATE TABLE reservations(
                origin TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('read','mutation')),
                admitted_utc REAL NOT NULL,
                resolved INTEGER NOT NULL DEFAULT 0 CHECK(resolved IN (0,1)),
                recovered INTEGER NOT NULL DEFAULT 0 CHECK(recovered IN (0,1)),
                post_cooldown INTEGER NOT NULL DEFAULT 0 CHECK(post_cooldown IN (0,1)),
                PRIMARY KEY(origin, attempt_id)
            );
            CREATE INDEX reservations_budget ON reservations(origin, admitted_utc);
            """
        )
        origin = "https://api.github.com"
        connection.execute("INSERT INTO governor_metadata VALUES (1, 1, ?)", (timestamp,))
        connection.execute("INSERT INTO origin_state(origin) VALUES (?)", (origin,))
        connection.execute("INSERT INTO reservations(origin, attempt_id, kind, admitted_utc) VALUES (?, 'legacy-attempt', 'read', ?)", (origin, timestamp))


def _initialize_and_send_process(path: str, channel: Connection, role: str) -> None:
    try:
        if role == "leader":
            original_initialize = GitHubRequestGovernor._initialize_or_migrate

            def held_initialize(governor: GitHubRequestGovernor) -> None:
                channel.send("initialization_held")
                assert channel.poll(10)
                assert channel.recv() == "release_initialization"
                original_initialize(governor)

            GitHubRequestGovernor._initialize_or_migrate = held_initialize
        else:
            original_flock = fcntl.flock
            initialization_lock_announced = False

            def observed_flock(fd: int, operation: int) -> None:
                nonlocal initialization_lock_announced
                if operation == fcntl.LOCK_EX and not initialization_lock_announced:
                    initialization_lock_announced = True
                    channel.send("initialization_lock_attempted")
                original_flock(fd, operation)

            fcntl.flock = observed_flock

        channel.send("constructing")
        # Coverage instrumentation can keep the leader transport paused for
        # longer than the ordinary five-second test budget. Keep the joiner
        # blocked long enough for the parent-controlled release rather than
        # turning scheduler slowness into a false admission-timeout failure.
        governor = GitHubRequestGovernor(store_path=Path(path), wait_budget=30)

        def handler(request: httpx.Request) -> httpx.Response:
            channel.send("transport_started")
            if role == "leader":
                assert channel.poll(10)
                assert channel.recv() == "release_transport"
            return httpx.Response(200, json={"ok": True}, request=request)

        with github_http_client(
            subsystem="controller-strict",
            transport=httpx.MockTransport(handler),
            admission_hook=governor.admit_blocking,
            observation_hook=governor.observe,
        ) as client:
            client.get(f"https://api.github.com/repos/{role}/widgets")
        channel.send("completed")
    except BaseException as exc:
        channel.send(("error", repr(exc)))


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
    """AS-003: a live join and premature close retain the reservation."""
    clock = Clock()
    path = tmp_path / "request_governor.sqlite3"
    first = GitHubRequestGovernor(monotonic=clock.monotonic, wall_time=clock.wall, store_path=path)
    assert first.admit(context(1, "mutation")) is True

    restarted = GitHubRequestGovernor(monotonic=clock.monotonic, wall_time=clock.wall, store_path=path)
    with pytest.raises(GitHubRequestDeferred) as live:
        restarted.admit(context(2))
    assert live.value.reason == "request_in_flight"
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT resolved, recovered FROM reservations WHERE attempt_id='attempt-1'").fetchone() == (0, 0)

    first.close()
    with pytest.raises(GitHubRequestDeferred) as still_live:
        restarted.admit(context(3))
    assert still_live.value.reason == "request_in_flight"
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT resolved, recovered FROM reservations WHERE attempt_id='attempt-1'").fetchone() == (0, 0)


def test_close_releases_only_an_incarnation_without_unresolved_work(tmp_path) -> None:
    clock = Clock()
    path = tmp_path / "incarnations.sqlite3"
    old = GitHubRequestGovernor(monotonic=clock.monotonic, wall_time=clock.wall, store_path=path)
    old_attempt = context(1)
    admit_and_finish(old, old_attempt)
    old.close()
    replacement = GitHubRequestGovernor(monotonic=clock.monotonic, wall_time=clock.wall, store_path=path)
    new_attempt = context(2)
    replacement.admit(new_attempt)

    # A stale completion carrying the same attempt identity cannot resolve a
    # reservation owned by another controller incarnation.
    stale = GitHubRequestGovernor(monotonic=clock.monotonic, wall_time=clock.wall, store_path=path)
    stale.observe(outcome(new_attempt))
    with pytest.raises(GitHubRequestDeferred) as still_live:
        stale.admit(context(3))
    assert still_live.value.reason == "request_in_flight"


def test_separate_process_lifetime_prevents_live_recovery(tmp_path) -> None:
    """A real process lifetime, rather than a PID lookup or heartbeat, is authoritative."""
    process_context = get_context("spawn")
    parent, child = process_context.Pipe()
    path = tmp_path / "process-shared.sqlite3"
    participant = process_context.Process(target=_hold_process_reservation, args=(str(path), child))
    participant.start()
    assert _receive(parent) == "admitted"
    survivor = GitHubRequestGovernor(store_path=path)
    try:
        with pytest.raises(GitHubRequestDeferred) as live:
            survivor.admit(context(901))
        assert live.value.reason == "request_in_flight"
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT recovered FROM reservations WHERE attempt_id='attempt-900'").fetchone() == (0,)

        participant.terminate()
        participant.join(timeout=5)
        assert not participant.is_alive()
        with pytest.raises(GitHubRequestDeferred) as recovered:
            survivor.admit(context(902))
        assert recovered.value.reason == "rate_limit_cooldown"
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT recovered FROM reservations WHERE attempt_id='attempt-900'").fetchone() == (1,)
    finally:
        if participant.is_alive():
            parent.send("stop")
            participant.join(timeout=5)


def test_simultaneous_first_use_converges_and_preserves_live_request(tmp_path) -> None:
    """REQ-001/003/004/005/007/008: real participants safely join one new store."""
    process_context = get_context("spawn")
    leader_parent, leader_child = process_context.Pipe()
    joiner_parent, joiner_child = process_context.Pipe()
    path = tmp_path / "simultaneous-first-use.sqlite3"
    leader = process_context.Process(target=_initialize_and_send_process, args=(str(path), leader_child, "leader"))
    joiner = process_context.Process(target=_initialize_and_send_process, args=(str(path), joiner_child, "joiner"))
    leader.start()
    try:
        assert _receive(leader_parent) == "constructing"
        assert _receive(leader_parent) == "initialization_held"
        joiner.start()
        assert _receive(joiner_parent) == "constructing"
        # This message is emitted immediately before the real blocking flock,
        # proving the joiner reached initialization while the leader held it.
        assert _receive(joiner_parent) == "initialization_lock_attempted"
        assert not joiner_parent.poll()

        leader_parent.send("release_initialization")
        assert _receive(leader_parent) == "transport_started"
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT schema_version FROM governor_metadata").fetchone() == (2,)
            # Coverage/instrumentation can issue an already-completed request
            # through the same client before the controlled transport blocks.
            # The concurrency invariant is that exactly one reservation remains
            # live and that it is not misclassified as recovered.
            assert connection.execute("SELECT resolved, recovered FROM reservations WHERE resolved=0").fetchall() == [(0, 0)]
            assert connection.execute("SELECT cooldown_until_utc, cooldown_reason FROM origin_state").fetchone() == (0.0, "")

        # The joiner is alive and initialized but cannot transmit while the
        # leader's production-boundary transport owns the shared reservation.
        assert not joiner_parent.poll()
        leader_parent.send("release_transport")
        assert _receive(leader_parent) == "completed"
        assert _receive(joiner_parent) == "transport_started"
        assert _receive(joiner_parent) == "completed"
        leader.join(timeout=5)
        joiner.join(timeout=5)
        assert leader.exitcode == 0
        assert joiner.exitcode == 0
        with sqlite3.connect(path) as connection:
            reservations = connection.execute("SELECT resolved, recovered FROM reservations ORDER BY admitted_utc").fetchall()
            assert len(reservations) >= 2
            assert set(reservations) == {(1, 0)}
    finally:
        if leader.is_alive():
            leader_parent.send("release_initialization")
            leader_parent.send("release_transport")
            leader.terminate()
        if joiner.is_alive():
            joiner.terminate()
        leader.join(timeout=5)
        if joiner.pid is not None:
            joiner.join(timeout=5)


def test_pre_schema_pragma_contention_recovers_same_governor_instance(tmp_path, monkeypatch) -> None:
    """REQ-003/008: transient pre-schema contention is retryable, never fail-open."""
    path = tmp_path / "pragma-contention.sqlite3"
    real_connect = sqlite3.connect
    contended_connections = 2

    class ContendedConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection

        def execute(self, statement: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
            nonlocal contended_connections
            if statement == "PRAGMA journal_mode=WAL" and contended_connections:
                contended_connections -= 1
                raise sqlite3.OperationalError("database is locked")
            return self.connection.execute(statement, parameters)

        def close(self) -> None:
            self.connection.close()

    def controlled_connect(*args: object, **kwargs: object) -> sqlite3.Connection | ContendedConnection:
        connection = real_connect(*args, **kwargs)
        return ContendedConnection(connection) if contended_connections else connection

    monkeypatch.setattr(sqlite3, "connect", controlled_connect)
    governor = GitHubRequestGovernor(store_path=path, wait_budget=0)
    sends: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sends.append(request.url.path)
        with real_connect(path) as observer:
            assert observer.execute("SELECT resolved, recovered FROM reservations").fetchall() == [(0, 0)]
        return httpx.Response(200, json={"ok": True}, request=request)

    def send(endpoint: str) -> None:
        with github_http_client(
            subsystem="controller-strict",
            transport=httpx.MockTransport(handler),
            admission_hook=governor.admit_blocking,
            observation_hook=governor.observe,
        ) as client:
            client.get(f"https://api.github.com{endpoint}")

    with pytest.raises(GitHubRequestDeferred) as contended:
        send("/first")
    assert contended.value.reason == "governor_initialization_contention"
    assert sends == []
    assert not path.exists() or path.stat().st_size == 0

    send("/second")
    assert sends == ["/second"]
    with real_connect(path) as observer:
        assert observer.execute("SELECT schema_version FROM governor_metadata").fetchone() == (2,)
        assert observer.execute("SELECT resolved, recovered FROM reservations").fetchall() == [(1, 0)]


def test_coordination_failure_does_not_release_a_live_boundary_transport(tmp_path, monkeypatch) -> None:
    """REQ-002/004/005/008: fail-closed state retains live lifetime evidence."""
    clock = Clock()
    path = tmp_path / "failed-owner.sqlite3"
    owner = GitHubRequestGovernor(monotonic=clock.monotonic, wall_time=clock.wall, store_path=path)
    survivor = GitHubRequestGovernor(monotonic=clock.monotonic, wall_time=clock.wall, store_path=path, wait_budget=0)
    entered = threading.Event()
    release = threading.Event()
    sends: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sends.append(request.url.path)
        if request.url.path == "/held":
            entered.set()
            release.wait(timeout=5)
        return httpx.Response(200, json={"ok": True}, request=request)

    def request(governor: GitHubRequestGovernor, endpoint: str) -> None:
        with github_http_client(
            subsystem="controller-strict",
            transport=httpx.MockTransport(handler),
            admission_hook=governor.admit_blocking,
            observation_hook=governor.observe,
        ) as client:
            client.get(f"https://api.github.com{endpoint}")

    first = threading.Thread(target=request, args=(owner, "/held"))
    first.start()
    assert entered.wait(timeout=5)

    class FailedTransaction:
        def __enter__(self) -> None:
            raise sqlite3.OperationalError("controlled persistence failure")

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(owner, "_transaction", lambda: FailedTransaction())
    with pytest.raises(GitHubRequestDeferred) as unavailable:
        request(owner, "/failed-admission")
    assert unavailable.value.reason == "governor_state_unavailable"

    clock.advance(120)
    with pytest.raises(GitHubRequestDeferred) as protected:
        request(survivor, "/must-not-send")
    assert protected.value.reason == "request_in_flight"
    assert sends == ["/held"]
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT recovered FROM reservations WHERE resolved=0").fetchone() == (0,)

    release.set()
    first.join(timeout=5)
    assert not first.is_alive()


def test_legacy_recovery_uses_logical_time_after_slow_initialization(tmp_path, monkeypatch) -> None:
    """REQ-009/017/020: migration starts recovery at its evaluation time."""
    clock = Clock()
    path = tmp_path / "legacy.sqlite3"
    _create_legacy_store(path, clock.wall_value)
    original_validate = GitHubRequestGovernor._validate_legacy_store

    def slow_validation(governor: GitHubRequestGovernor, tables: set[str]) -> None:
        original_validate(governor, tables)
        clock.advance(120)

    monkeypatch.setattr(GitHubRequestGovernor, "_validate_legacy_store", slow_validation)
    upgraded = GitHubRequestGovernor(monotonic=clock.monotonic, wall_time=clock.wall, store_path=path)

    with pytest.raises(GitHubRequestDeferred) as recovery:
        upgraded.admit(context(903))
    assert recovery.value.reason == "rate_limit_cooldown"
    assert recovery.value.retry_at == clock.wall_value + 60
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT schema_version, last_logical_utc FROM governor_metadata").fetchone() == (2, clock.wall_value)
        assert connection.execute("SELECT cooldown_until_utc, cooldown_reason FROM origin_state").fetchone() == (
            clock.wall_value + 60,
            "unresolved_attempt_recovery",
        )


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
