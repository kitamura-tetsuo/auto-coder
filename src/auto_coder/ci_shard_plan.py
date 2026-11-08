"""Immutable, complete pytest shard plans with explicit checkout/profile identity."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import cast

from auto_coder.ci_duration_profile import ProfileIdentity, ProfileInputError, _previous

CONTEXT_SCHEMA = "ci-shard-context-v1"
INVENTORY_SCHEMA = "ci-shard-inventory-v1"
PLAN_SCHEMA = "ci-shard-plan-v1"
TARGET_MS = 420_000
SHARD_CAP = 12
PLUGIN = "scripts.ci_shard_plugin"
MANDATORY_ENVIRONMENT = ("PYTEST_ADDOPTS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "PYTEST_PLUGINS")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


class ShardPlanError(ValueError):
    """A plan cannot safely represent the complete tested pytest selection."""


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ShardPlanError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _constant(value: str) -> object:
    raise ShardPlanError(f"nonfinite JSON constant: {value}")


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ShardPlanError(f"{name} must be an object")
    return cast(dict[str, object], value)


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ShardPlanError(f"{name} must be a nonempty string")
    return value


def _strings(value: object, name: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise ShardPlanError(f"{name} must be a list")
    return tuple(_string(item, name) for item in value)


def _number(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ShardPlanError(f"{name} must be a positive integer")
    return value


def _canonical(data: object) -> bytes:
    return (json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def _digest(data: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(data)).hexdigest()


def _read(path: Path) -> dict[str, object]:
    try:
        return _object(json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs, parse_constant=_constant), str(path))
    except (UnicodeError, OSError, ValueError, TypeError) as error:
        raise ShardPlanError(f"{path}: unreadable JSON: {error}") from error


@dataclass(frozen=True)
class ShardContext:
    repository: str
    profile_id: str
    checkout_sha: str
    pytest_args: tuple[str, ...]
    harness_files: tuple[str, ...]
    semantic_environment: tuple[str, ...]
    origin: dict[str, object]

    @classmethod
    def parse(cls, value: object) -> ShardContext:
        data = _object(value, "context")
        if data.get("schema_version") != CONTEXT_SCHEMA:
            raise ShardPlanError(f"context requires {CONTEXT_SCHEMA}")
        repository = _string(data.get("repository"), "repository")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ShardPlanError("repository must be owner/name")
        sha = _string(data.get("checkout_sha"), "checkout_sha")
        if not SHA_PATTERN.fullmatch(sha):
            raise ShardPlanError("checkout_sha must be an actual Git SHA")
        args = _strings(data.get("pytest_args"), "pytest_args")
        harness = _strings(data.get("harness_files"), "harness_files")
        env = _strings(data.get("semantic_environment"), "semantic_environment")
        if len(set(harness)) != len(harness) or len(set(env)) != len(env):
            raise ShardPlanError("harness_files and semantic_environment cannot contain duplicates")
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in env):
            raise ShardPlanError("invalid semantic environment variable name")
        origin = _object(data.get("origin"), "origin")
        if origin.get("repository") != repository:
            raise ShardPlanError("origin.repository differs from tested repository")
        for name in ("workflow", "run_id", "run_attempt", "planning_attempt", "event_sha"):
            if origin.get(name) is not None and not isinstance(origin.get(name), str):
                raise ShardPlanError(f"origin.{name} must be a string or null")
        if origin.get("event_sha") is not None and not SHA_PATTERN.fullmatch(_string(origin["event_sha"], "origin.event_sha")):
            raise ShardPlanError("invalid event_sha")
        return cls(repository, _string(data.get("profile_id"), "profile_id"), sha, args, harness, env, dict(origin))

    @classmethod
    def read(cls, path: Path) -> ShardContext:
        return cls.parse(_read(path))

    def semantic_json(self) -> dict[str, object]:
        return {
            "schema_version": CONTEXT_SCHEMA,
            "repository": self.repository,
            "profile_id": self.profile_id,
            "checkout_sha": self.checkout_sha,
            "pytest_args": list(self.pytest_args),
            "harness_files": list(self.harness_files),
            "semantic_environment": list(self.semantic_environment),
        }

    def json(self) -> dict[str, object]:
        return {**self.semantic_json(), "origin": self.origin}


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=False, timeout=15)
    if result.returncode != 0:
        raise ShardPlanError(f"Git identity check failed: git {' '.join(args[:2])}")
    return result.stdout.strip()


def _tracked_hash(root: Path, name: str) -> str:
    path = Path(name)
    if path.is_absolute() or not path.parts or any(part in ("..", ".") for part in path.parts) or path.as_posix() != name:
        raise ShardPlanError(f"invalid relative tracked harness path: {name}")
    source = root / path
    if source.is_symlink() or not source.is_file() or _git(root, "ls-files", "--error-unmatch", "-z", "--", name) != name + "\0":
        raise ShardPlanError(f"harness file is missing or not tracked: {name}")
    return "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()


def _runtime(root: Path, context: ShardContext, actual_args: tuple[str, ...], config_path: str | None, pytest_rootdir: str) -> dict[str, object]:
    if Path.cwd().resolve() != root.resolve():
        raise ShardPlanError("pytest must run from the tested repository root")
    sha = _git(root, "rev-parse", "HEAD")
    if sha != context.checkout_sha:
        raise ShardPlanError(f"tested checkout differs: expected {context.checkout_sha}, found {sha}")
    if _git(root, "status", "--porcelain", "--untracked-files=no"):
        raise ShardPlanError("tested checkout contains tracked changes not represented by its Git HEAD")
    if actual_args != context.pytest_args:
        raise ShardPlanError(f"pytest selector/configuration mismatch: expected {context.pytest_args}, found {actual_args}")
    if config_path is not None:
        resolved = Path(config_path).resolve()
        if not resolved.is_relative_to(root.resolve()):
            raise ShardPlanError("pytest configuration escapes tested checkout")
        config_name = str(resolved.relative_to(root.resolve()))
    else:
        config_name = None
    files = {name: "sha256:" + hashlib.sha256((root / name).read_bytes()).hexdigest() for name in ("uv.lock", "pyproject.toml")}
    files.update({name: _tracked_hash(root, name) for name in sorted(context.harness_files)})
    names = sorted(set(context.semantic_environment).union(MANDATORY_ENVIRONMENT))
    return {
        "pytest_args": list(actual_args),
        "pytest_config_path": config_name,
        "pytest_rootdir": os.path.relpath(Path(pytest_rootdir).resolve(), root),
        "python_major_minor": f"{sys.version_info.major}.{sys.version_info.minor}",
        "files": files,
        "semantic_environment": {name: os.environ.get(name) for name in names},
    }


def runtime_profile(context: ShardContext, actual_args: tuple[str, ...], config_path: str | None, pytest_rootdir: str) -> dict[str, object]:
    root = Path(_git(Path.cwd(), "rev-parse", "--show-toplevel")).resolve()
    configuration = _runtime(root, context, actual_args, config_path, pytest_rootdir)
    return ProfileIdentity(context.repository, context.profile_id, _digest(configuration), cast(str, configuration["python_major_minor"])).json()


def semantic_pytest_args(args: tuple[str, ...]) -> tuple[str, ...]:
    """Strip only this adapter's controls and explicitly permitted report flags."""
    result: list[str] = []
    index = 0
    value_options = {"--ci-collect-output", "--ci-context", "--ci-plan", "--ci-shard", "--junitxml", "--junit-xml", "--color"}
    while index < len(args):
        arg = args[index]
        if arg == "-p" and index + 1 < len(args) and args[index + 1] == PLUGIN:
            index += 2
        elif arg in value_options:
            if index + 1 >= len(args):
                raise ShardPlanError(f"missing value for {arg}")
            index += 2
        elif any(arg.startswith(option + "=") for option in value_options):
            index += 1
        elif arg in ("--collect-only", "-q", "-v", "-vv", "-vvv"):
            index += 1
        else:
            result.append(arg)
            index += 1
    return tuple(result)


def validate_inventory(nodeids: tuple[str, ...], *, required: bool = True) -> None:
    if required and not nodeids:
        raise ShardPlanError("required profile collected no tests")
    if any(not isinstance(node, str) or not node.strip() for node in nodeids) or len(set(nodeids)) != len(nodeids):
        raise ShardPlanError("empty or duplicate pytest node IDs in selected inventory")


def _weight(seconds: object) -> int:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise ShardPlanError("invalid measured estimate")
    try:
        value = float(seconds)
    except (OverflowError, ValueError) as error:
        raise ShardPlanError("invalid measured estimate") from error
    if not math.isfinite(value) or value <= 0:
        raise ShardPlanError("measured estimate must be finite and positive")
    # A finite native P95 can exceed float's range after conversion to ms.
    # Decimal keeps that legitimate indivisible work instead of overflowing.
    return max(1, int((Decimal(str(value)) * 1000).to_integral_value(rounding=ROUND_CEILING)))


def duration_estimates(path: Path | None, profile: dict[str, object], inventory: tuple[str, ...]) -> tuple[list[dict[str, object]], dict[str, object]]:
    known: dict[str, tuple[int, int]] = {}
    source: dict[str, object] = {"status": "unavailable", "digest": None, "reason": "no duration profile supplied"}
    if path is not None:
        try:
            identity = ProfileIdentity.parse(profile)
            # The native materializer's parser checks its canonical snapshot digest,
            # origin, sample identities, native durations, and P95 consistency.
            _previous(path, identity)
            data = _read(path)
            nodes = _object(data.get("nodes"), "duration.nodes")
            for node in inventory:
                if node in nodes:
                    entry = _object(nodes[node], f"duration.nodes.{node}")
                    known[node] = (_weight(entry.get("p95_seconds")), _number(entry.get("sample_count"), "sample_count"))
            source = {"status": "matched", "digest": "sha256:" + _string(data.get("digest_sha256"), "digest_sha256"), "reason": None}
        except (ProfileInputError, ShardPlanError, OSError, ValueError) as error:
            source = {"status": "unavailable", "digest": None, "reason": f"duration profile ignored: {type(error).__name__}: {error}"}
            known.clear()
    fallback = max(1000, max((item[0] for item in known.values()), default=0))
    estimate: list[dict[str, object]] = [
        {"nodeid": node, "estimated_ms": known[node][0], "provenance": "native_p95", "sample_count": known[node][1]} if node in known else {"nodeid": node, "estimated_ms": fallback, "provenance": "fallback_current_max_p95" if known else "fallback_one_second", "sample_count": 0} for node in inventory
    ]
    return estimate, source


def _groups(weights: tuple[int, ...], capacity: int) -> list[tuple[int, int]]:
    groups: list[tuple[int, int]] = []
    start = 0
    running = 0
    for position, weight in enumerate(weights):
        if position > start and running + weight > capacity:
            groups.append((start, position))
            start = position
            running = 0
        running += weight
    if weights:
        groups.append((start, len(weights)))
    return groups


def allocate(inventory: tuple[str, ...], estimates: list[dict[str, object]]) -> tuple[list[dict[str, object]], int, bool]:
    """Minimum contiguous groups at target; binary-search capacity only for the cap."""
    validate_inventory(inventory)
    if [entry.get("nodeid") for entry in estimates] != list(inventory):
        raise ShardPlanError("estimates do not match ordered complete inventory")
    weights = tuple(_number(item.get("estimated_ms"), "estimated_ms") for item in estimates)
    # An indivisible long node is a full shard even above the target. Its
    # presence must not relax the target for unrelated, normally sized nodes.
    capacity = TARGET_MS
    capacity_limited = len(_groups(weights, capacity)) > SHARD_CAP
    if capacity_limited:
        left, right = capacity, sum(weights)
        while left < right:
            midpoint = (left + right) // 2
            if len(_groups(weights, midpoint)) <= SHARD_CAP:
                right = midpoint
            else:
                left = midpoint + 1
        capacity = left
    shards = []
    for number, (first, end) in enumerate(_groups(weights, capacity), 1):
        shards.append({"id": number, "nodeids": list(inventory[first:end]), "estimated_ms": sum(weights[first:end]), "over_budget_nodes": [node for node, ms in zip(inventory[first:end], weights[first:end]) if ms > TARGET_MS]})
    return shards, capacity, capacity_limited


def build_plan(context: ShardContext, profile: dict[str, object], inventory: tuple[str, ...], duration_path: Path | None) -> dict[str, object]:
    validate_inventory(inventory)
    estimates, source = duration_estimates(duration_path, profile, inventory)
    shards, capacity, limited = allocate(inventory, estimates)
    semantic = {
        "context": context.semantic_json(),
        "profile": profile,
        "inventory": list(inventory),
        "inventory_digest": _digest(list(inventory)),
        "estimates": estimates,
        "duration_source": source,
        "budget_ms": TARGET_MS,
        "shard_cap": SHARD_CAP,
        "effective_capacity_ms": capacity,
        "capacity_limited": limited,
        "predicted_total_ms": sum(cast(int, entry["estimated_ms"]) for entry in estimates),
        "shards": shards,
    }
    return {"schema_version": PLAN_SCHEMA, "semantic": semantic, "digest_sha256": _digest(semantic), "origin": context.origin}


@dataclass(frozen=True)
class VerifiedPlan:
    digest: str
    context: ShardContext
    profile: dict[str, object]
    inventory: tuple[str, ...]
    shards: tuple[tuple[str, ...], ...]


def verify_plan(data: dict[str, object]) -> VerifiedPlan:
    if data.get("schema_version") != PLAN_SCHEMA:
        raise ShardPlanError("unsupported shard plan schema")
    semantic = _object(data.get("semantic"), "semantic")
    digest = _string(data.get("digest_sha256"), "digest_sha256")
    if not DIGEST_PATTERN.fullmatch(digest) or _digest(semantic) != digest:
        raise ShardPlanError("plan semantic digest mismatch")
    raw_context = _object(semantic.get("context"), "semantic.context")
    origin = _object(data.get("origin"), "origin")
    context = ShardContext.parse({**raw_context, "origin": origin})
    if raw_context != context.semantic_json():
        raise ShardPlanError("unexpected semantic context fields")
    profile = _object(semantic.get("profile"), "profile")
    identity = ProfileIdentity.parse(profile)
    if profile != identity.json() or identity.repository != context.repository or identity.profile_id != context.profile_id:
        raise ShardPlanError("plan profile does not match context")
    inventory = _strings(semantic.get("inventory"), "inventory", allow_empty=False)
    validate_inventory(inventory)
    if semantic.get("inventory_digest") != _digest(list(inventory)):
        raise ShardPlanError("plan inventory digest mismatch")
    if semantic.get("budget_ms") != TARGET_MS or semantic.get("shard_cap") != SHARD_CAP:
        raise ShardPlanError("plan budget/cap altered")
    estimates_raw = semantic.get("estimates")
    if not isinstance(estimates_raw, list):
        raise ShardPlanError("plan estimates missing")
    estimates = [_object(item, "estimate") for item in estimates_raw]
    for estimate in estimates:
        _number(estimate.get("estimated_ms"), "estimated_ms")
        if estimate.get("provenance") not in ("native_p95", "fallback_current_max_p95", "fallback_one_second"):
            raise ShardPlanError("unknown estimate provenance")
        if isinstance(estimate.get("sample_count"), bool) or not isinstance(estimate.get("sample_count"), int) or cast(int, estimate["sample_count"]) < 0:
            raise ShardPlanError("invalid estimate sample count")
    shards, capacity, limited = allocate(inventory, estimates)
    if semantic.get("shards") != shards or semantic.get("effective_capacity_ms") != capacity or semantic.get("capacity_limited") != limited:
        raise ShardPlanError("plan assignments violate deterministic complete partition")
    if semantic.get("predicted_total_ms") != sum(cast(int, item["estimated_ms"]) for item in estimates):
        raise ShardPlanError("plan predicted total mismatch")
    return VerifiedPlan(digest, context, profile, inventory, tuple(tuple(cast(list[str], shard["nodeids"])) for shard in shards))


def write_plan(path: Path, plan: dict[str, object], protected: tuple[Path, ...]) -> None:
    """Exclusive immutable publication, without replacing any prior plan/input."""
    verify_plan(plan)
    if path.resolve() in {item.resolve() for item in protected} or path.exists() or path.is_symlink():
        raise ShardPlanError("plan output already exists or aliases an input")
    temporary: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False) as stream:
            temporary = stream.name
            stream.write(_canonical(plan))
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    except OSError as error:
        raise ShardPlanError(f"atomic plan publication failed: {error}") from error
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def read_plan(path: Path) -> tuple[VerifiedPlan, bytes]:
    try:
        raw = path.read_bytes()
        data = _object(json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant), str(path))
    except (OSError, UnicodeError, ValueError) as error:
        raise ShardPlanError(f"invalid plan: {error}") from error
    if _canonical(data) != raw:
        raise ShardPlanError("plan is not in canonical immutable form")
    return verify_plan(data), raw
