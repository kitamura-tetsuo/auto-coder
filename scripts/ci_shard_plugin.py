"""Explicit pytest collect-only inventory and immutable shard application adapter."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from auto_coder.ci_shard_plan import INVENTORY_SCHEMA, ShardContext, ShardPlanError, VerifiedPlan, _canonical, _digest, read_plan, runtime_profile, semantic_pytest_args, validate_inventory

STATE: pytest.StashKey[Adapter | None] = pytest.StashKey()


@dataclass
class Adapter:
    mode: str
    output: Path | None
    plan_path: Path | None
    plan_bytes: bytes | None
    plan: VerifiedPlan | None
    shard: int | None
    context: ShardContext
    profile: dict[str, object]
    collected: tuple[str, ...] = ()
    selected: tuple[str, ...] = ()


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("ci-shard", "Complete time-budgeted CI shard plans")
    group.addoption("--ci-context", help="Profile context for real machine-readable collect-only mode")
    group.addoption("--ci-collect-output", help="Exclusive JSON inventory output; requires --collect-only")
    group.addoption("--ci-plan", help="Immutable plan JSON to apply (without --collect-only)")
    group.addoption("--ci-shard", help="One-based shard ID from --ci-plan")


def _assert_plan_unchanged(state: Adapter) -> None:
    if state.plan_path is None or state.plan_bytes is None:
        return
    try:
        if state.plan_path.read_bytes() != state.plan_bytes:
            raise ShardPlanError("plan changed during this pytest execution")
    except OSError as error:
        raise ShardPlanError(f"plan became unreadable during execution: {error}") from error


def _state(config: pytest.Config) -> Adapter | None:
    return config.stash.get(STATE, None)


def _invocation(config: pytest.Config) -> tuple[str, ...]:
    return semantic_pytest_args(tuple(str(arg) for arg in config.invocation_params.args))


def _config_path(config: pytest.Config) -> str | None:
    return str(config.inipath) if config.inipath is not None else None


def pytest_configure(config: pytest.Config) -> None:
    context_file = config.getoption("--ci-context")
    collect_file = config.getoption("--ci-collect-output")
    plan_file = config.getoption("--ci-plan")
    shard_value = config.getoption("--ci-shard")
    if not any((context_file, collect_file, plan_file, shard_value)):
        config.stash[STATE] = None
        return
    # pytest initial root/config discovery precedes registration of these options.
    # Separated values can be mistaken for collection paths; fail closed.
    if any(arg in ("--ci-context", "--ci-collect-output", "--ci-plan", "--ci-shard") for arg in config.invocation_params.args):
        raise pytest.UsageError("CI shard controls require --ci-name=value syntax to preserve original pytest configuration discovery")
    try:
        if collect_file and context_file and not plan_file and not shard_value:
            if not config.getoption("collectonly"):
                raise ShardPlanError("inventory mode requires real pytest --collect-only")
            context = ShardContext.read(Path(context_file))
            profile = runtime_profile(context, _invocation(config), _config_path(config), str(config.rootpath))
            config.stash[STATE] = Adapter("collect", Path(collect_file), None, None, None, None, context, profile)
        elif plan_file and shard_value and not collect_file and not context_file:
            if config.getoption("collectonly"):
                raise ShardPlanError("shard application cannot use --collect-only")
            plan, raw = read_plan(Path(plan_file))
            try:
                shard = int(shard_value)
            except ValueError as error:
                raise ShardPlanError("shard ID must be a positive integer") from error
            if shard < 1 or shard > len(plan.shards) or str(shard) != str(shard_value):
                raise ShardPlanError("shard ID is not in the immutable plan")
            profile = runtime_profile(plan.context, _invocation(config), _config_path(config), str(config.rootpath))
            if profile != plan.profile:
                raise ShardPlanError("effective profile fingerprint differs from plan")
            config.stash[STATE] = Adapter("apply", None, Path(plan_file), raw, plan, shard, plan.context, profile)
        else:
            raise ShardPlanError("choose either --ci-context/--ci-collect-output or --ci-plan/--ci-shard")
    except (ShardPlanError, OSError, ValueError) as error:
        raise pytest.UsageError(f"CI shard refused: {error}") from error


def _publish_inventory(path: Path, payload: dict[str, object]) -> None:
    if path.exists() or path.is_symlink():
        raise ShardPlanError("inventory output already exists")
    temporary: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False) as stream:
            temporary = stream.name
            stream.write(_canonical(payload))
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    except OSError as error:
        raise ShardPlanError(f"inventory output failed: {error}") from error
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_collection_finish(session: pytest.Session):  # type: ignore[no-untyped-def]
    # The wrapper's teardown observes the collection after selector and other
    # collection-finish hooks, before the first native test is invoked.
    yield
    state = _state(session.config)
    if state is None:
        return
    try:
        nodeids = tuple(item.nodeid for item in session.items)
        validate_inventory(nodeids)
        if state.mode == "collect":
            assert state.output is not None
            _publish_inventory(
                state.output,
                {"schema_version": INVENTORY_SCHEMA, "checkout_sha": state.context.checkout_sha, "profile": state.profile, "pytest_args": list(state.context.pytest_args), "nodeids": list(nodeids), "inventory_digest": _digest(list(nodeids))},
            )
            return
        _assert_plan_unchanged(state)
        assert state.plan is not None and state.shard is not None
        if nodeids != state.plan.inventory:
            raise ShardPlanError("complete post-selector inventory differs from immutable plan (added, deleted, reordered or duplicate test)")
        state.collected = nodeids
        selected = state.plan.shards[state.shard - 1]
        selected_set = set(selected)
        keep = [item for item in session.items if item.nodeid in selected_set]
        if tuple(item.nodeid for item in keep) != selected:
            raise ShardPlanError("selected shard does not match its original ordered inventory")
        dropped = [item for item in session.items if item.nodeid not in selected_set]
        session.items[:] = keep
        session.config.hook.pytest_deselected(items=dropped)
        state.selected = selected
        terminal = session.config.pluginmanager.get_plugin("terminalreporter")
        if terminal is not None:
            terminal.write_line(f"CI shard {state.shard}/{len(state.plan.shards)} plan={state.plan.digest} selected={len(selected)} nodes={json.dumps(selected)}")
    except (ShardPlanError, OSError, ValueError) as error:
        raise pytest.UsageError(f"CI shard refused before test execution: {error}") from error


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtestloop(session: pytest.Session):  # type: ignore[no-untyped-def]
    state = _state(session.config)
    if state is not None and state.mode == "apply":
        try:
            _assert_plan_unchanged(state)
            if tuple(item.nodeid for item in session.items) != state.selected:
                raise ShardPlanError("final selected inventory altered after shard application")
        except (ShardPlanError, OSError) as error:
            raise pytest.UsageError(f"CI shard refused before first test: {error}") from error
    yield


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    state = _state(session.config)
    if state is not None and state.mode == "apply":
        try:
            _assert_plan_unchanged(state)
        except ShardPlanError as error:
            if session.exitstatus == pytest.ExitCode.OK:
                session.exitstatus = pytest.ExitCode.TESTS_FAILED
            terminal = session.config.pluginmanager.get_plugin("terminalreporter")
            if terminal is not None:
                terminal.write_line(f"CI shard plan integrity failure: {error}", red=True)
