"""One explicit native mutation control, separate from production acceptance.

The control starts from a private project copy before any original fixture is
created. It removes only the post-consumption final verifier, so the real
package identity, Docker images, history and enrollment all agree on that
intentionally incorrect copy. No production source or authority provider is
patched at runtime.
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

from loguru import logger

CONTROL = "omit-post-consumption-verifier"
CONTROL_SCENARIO = "post-receipt-ready-mutation"
CONTROL_SCHEMA = "auto-coder-native-recovery-mutation-control-result-v1"
SOURCE_SCHEMA = "auto-coder-recovery-mutation-source-v1"
ORACLE = "post-receipt-grant-forbidden"
ISSUER_SOURCE = Path("src/auto_coder/management/recovery_epoch.py")
_IGNORED = frozenset({".git", ".venv", ".agent-tmp", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"})


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def omit_post_consumption_verifier(source: bytes) -> tuple[bytes, int]:
    """Delete exactly the second standalone call, proving the full AST delta."""
    original = ast.parse(source)
    definitions = [node for node in original.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "issue_epoch_once"]
    if len(definitions) != 1:
        raise ValueError("the actual source has no unique issue_epoch_once function")
    function = definitions[0]
    calls = sorted((node for node in ast.walk(function) if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) and node.value.func.id == "final_verification"), key=lambda node: node.lineno)
    if len(calls) != 2 or any(node.value.args or node.value.keywords for node in calls):
        raise ValueError("the actual issuer does not contain exactly two standalone final verifier calls")
    first, removed = calls
    parents = [node for node in ast.walk(function) if isinstance(getattr(node, "body", None), list) and removed in node.body]
    if len(parents) != 1:
        raise ValueError("the second final verifier has no unique original statement list")
    statements = parents[0].body
    index = statements.index(removed)
    previous = statements[index - 1] if index else None
    if not (
        isinstance(previous, ast.Assign)
        and len(previous.targets) == 1
        and isinstance(previous.targets[0], ast.Name)
        and previous.targets[0].id == "receipt"
        and isinstance(previous.value, ast.Call)
        and isinstance(previous.value.func, ast.Attribute)
        and isinstance(previous.value.func.value, ast.Name)
        and (previous.value.func.value.id, previous.value.func.attr) == ("state", "consume")
        and len(previous.value.args) == 1
        and isinstance(previous.value.args[0], ast.Name)
        and previous.value.args[0].id == "binding"
        and not previous.value.keywords
        and first.lineno < previous.lineno < removed.lineno
    ):
        raise ValueError("the selected second verifier does not directly follow actual durable consumption")
    lines = source.splitlines(keepends=True)
    if removed.end_lineno != removed.lineno or lines[removed.lineno - 1].strip() != b"final_verification()":
        raise ValueError("the second verifier is not one exact removable source statement")
    mutant = b"".join(lines[: removed.lineno - 1] + lines[removed.lineno :])
    expected = copy.deepcopy(original)
    expected_function = next(node for node in expected.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "issue_epoch_once")
    expected_removed = next(node for node in ast.walk(expected_function) if isinstance(node, ast.Expr) and node.lineno == removed.lineno)
    expected_parent = next(node for node in ast.walk(expected_function) if isinstance(getattr(node, "body", None), list) and expected_removed in node.body)
    expected_parent.body.remove(expected_removed)
    if ast.dump(ast.parse(mutant), include_attributes=False) != ast.dump(expected, include_attributes=False):
        raise ValueError("the native mutation changed more than the second final verifier")
    return mutant, removed.lineno


def _files(root: Path) -> dict[str, str]:
    result = {}
    for directory, names, files in os.walk(root):
        names[:] = sorted(name for name in names if name not in _IGNORED)
        if any((Path(directory) / name).is_symlink() for name in names):
            raise ValueError("the mutation source contains a linked directory")
        for name in sorted(files):
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                raise ValueError("the mutation source contains a nonregular file: " + str(path))
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def prepare(source: Path, destination: Path, evidence: Path) -> dict[str, object]:
    """Create one distinct source copy; never overwrite or modify the original."""
    source = source.resolve(strict=True)
    if not source.is_dir() or not destination.is_absolute() or destination.exists() or destination.is_symlink() or destination.is_relative_to(source):
        raise ValueError("a new separate absolute mutation-copy directory is required")
    if not evidence.is_absolute() or evidence.exists() or evidence.is_symlink() or evidence.is_relative_to(source) or evidence.is_relative_to(destination):
        raise ValueError("new mutation evidence must be outside both source trees")
    original_files = _files(source)
    original = (source / ISSUER_SOURCE).read_bytes()
    mutant, line = omit_post_consumption_verifier(original)
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(*_IGNORED), copy_function=shutil.copy2)
    selected = destination / ISSUER_SOURCE
    with selected.open("wb") as stream:
        stream.write(mutant)
        stream.flush()
        os.fsync(stream.fileno())
    copied_files = _files(destination)
    changed = [path for path in original_files.keys() | copied_files.keys() if original_files.get(path) != copied_files.get(path)]
    if changed != [ISSUER_SOURCE.as_posix()] or _files(source) != original_files:
        raise AssertionError("the copied project differs outside the single approved verifier statement")
    result = {
        "schema": SOURCE_SCHEMA,
        "control": CONTROL,
        "scenario": CONTROL_SCENARIO,
        "source_root": str(source),
        "control_root": str(destination),
        "changed_file": ISSUER_SOURCE.as_posix(),
        "removed_call_lineno": line,
        "original_source_sha256": hashlib.sha256(original).hexdigest(),
        "mutant_source_sha256": hashlib.sha256(mutant).hexdigest(),
        "original_project_sha256": hashlib.sha256(_canonical(original_files)).hexdigest(),
        "mutant_project_sha256": hashlib.sha256(_canonical(copied_files)).hexdigest(),
        "package_files": {path.removeprefix("src/auto_coder/"): digest for path, digest in copied_files.items() if path.startswith("src/auto_coder/") and path.endswith(".py")},
        "fixture_files": {path: digest for path, digest in copied_files.items() if path.startswith(("scripts/", "tests/")) and path.endswith(".py")},
        "only_second_verifier_removed": True,
        "first_verifier_retained": True,
    }
    descriptor = os.open(evidence, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(_canonical(result))
        stream.flush()
        os.fsync(stream.fileno())
    logger.info("Prepared the isolated source mutation at original line {}: {}", line, selected)
    return result


def verify_source_evidence(path: Path, *, executing: bool = False) -> tuple[dict[str, object], str]:
    encoded = path.read_bytes()
    value = json.loads(encoded)
    if _canonical(value) != encoded or value.get("schema") != SOURCE_SCHEMA or value.get("control") != CONTROL or value.get("scenario") != CONTROL_SCENARIO:
        raise ValueError("the mutation source evidence is not the exact supported canonical record")
    original = Path(value["source_root"]) / ISSUER_SOURCE
    changed = Path(value["control_root"]) / ISSUER_SOURCE
    mutant, line = omit_post_consumption_verifier(original.read_bytes())
    if changed.read_bytes() != mutant or value["removed_call_lineno"] != line or value["original_source_sha256"] != hashlib.sha256(original.read_bytes()).hexdigest() or value["mutant_source_sha256"] != hashlib.sha256(mutant).hexdigest():
        raise AssertionError("the retained original or exact single-call mutation changed")
    if value.get("only_second_verifier_removed") is not True or value.get("first_verifier_retained") is not True:
        raise AssertionError("the mutation omitted its explicit single-verifier proof")
    if executing:
        from auto_coder.management import recovery_epoch

        package = Path(recovery_epoch.__file__).resolve(strict=True).parents[1]
        actual = {item.relative_to(package).as_posix(): hashlib.sha256(item.read_bytes()).hexdigest() for item in sorted(package.rglob("*.py")) if not item.is_symlink()}
        if actual != value["package_files"]:
            raise AssertionError("the executing issuer package is not the complete original mutation copy")
        if Path(__file__).resolve(strict=True).parents[1] != Path(value["control_root"]):
            raise AssertionError("the actual fixture scripts are not running from the private copied project")
        if any(hashlib.sha256((Path(value["control_root"]) / name).read_bytes()).hexdigest() != digest for name, digest in value["fixture_files"].items()):
            raise AssertionError("the copied native fixture scripts changed after the single-call mutation")
    return value, hashlib.sha256(encoded).hexdigest()


def _validate_queued_grant(queued: dict[str, object]) -> None:
    if queued.get("schema") != "actual-recovery-queued-grant-v1" or queued.get("grant_schema") != "management-recovery-epoch-grant-v1":
        raise AssertionError("the mutation did not observe the actual canonical recovery grant")
    grant = queued["grant"]
    assert set(grant) == {"schema", "granted", "request_id", "authorization_id", "target_id", "challenge", "receipt_nonce", "inventory_sha256", "binding_sha256", "consumed_sha256", "receipt_sha256", "host_boot_id", "host_pid", "host_start_token"}
    assert grant["schema"] == queued["grant_schema"] and grant["granted"] == "recovery-epoch-consumed"
    assert all(re.fullmatch(r"[0-9a-f]{64}", grant[name]) for name in ("challenge", "receipt_nonce", "inventory_sha256", "binding_sha256", "consumed_sha256", "receipt_sha256"))
    encoded = _canonical(grant) + b"\n"
    assert queued["bytes_observed"] == len(encoded) and queued["message_sha256"] == hashlib.sha256(encoded).hexdigest()
    assert all(queued.get(name) is True for name in ("peek_only", "socket_flags_unchanged", "bytes_still_queued", "receiver_stopped", "issuer_alive"))
    assert queued["socket_flags_before"] == queued["socket_flags_after"] and queued["socket_inode"] > 0
    assert queued["peer_credentials"] == {"pid": queued["issuer_pid"], "uid": 0, "gid": 0}
    assert (queued["grant"]["target_id"], queued["grant"]["host_pid"], queued["grant"]["host_start_token"]) == (queued["target_id"], queued["issuer_pid"], queued["issuer_start_token"])


class PostReceiptGrantDispatched(AssertionError):
    """The same native oracle observed the precise incorrect dispatched frame."""


def assert_post_receipt_refusal(observation: dict[str, object]) -> None:
    assert observation["schema"] == "actual-recovery-post-receipt-dispatch-v1"
    assert observation["receiver_stopped"] is True and observation["queue_empty_before_resume"] is True and observation["socket_flags_unchanged"] is True
    queued = observation["queued_grant"]
    if queued is not None:
        _validate_queued_grant(queued)
        assert observation["queue_bytes_observed"] == queued["bytes_observed"]
        assert all(observation[name] == queued[name] for name in ("issuer_pid", "issuer_start_token", "target_pid", "target_start_token", "peer_credentials"))
        raise PostReceiptGrantDispatched(ORACLE)
    assert observation["queue_bytes_observed"] == 0 and observation["issuer_returncode"] == 2


def validate_control_result(value: dict[str, object], evidence: Path) -> None:
    source, digest = verify_source_evidence(evidence)
    assert value["schema"] == CONTROL_SCHEMA and value["status"] == "mutation-detected" and value["control"] == CONTROL and value["scenario"] == CONTROL_SCENARIO
    assert value["source_evidence_sha256"] == digest and value["source_mutation"] == source
    assert value["no_child_started"] is True and value["preserved_originals"] is True and value["preserved_workers"] is True
    assert value["original_outcomes"]["D"] == "never-created" and value["original_outcomes"]["C"] in {"launched", "unknown"}
    oracle = value["oracle"]
    assert oracle["name"] == ORACLE and oracle["detected"] is True
    _validate_queued_grant(oracle["queued_grant"])
    durable = value["durable"]
    assert durable["phase"] == "receipt" and durable["consumer_grant_acknowledged"] is False and durable["grant_send_completed"] is True
    assert durable["mutation"]["scenario"] == CONTROL_SCENARIO and durable["mutation"]["boundary"] == "after-receipt-head"
    assert durable["dispatch"]["queued_grant"] == oracle["queued_grant"]
    assert durable["dispatch"]["issuer_death_before_receiver_resumed"] is True and durable["replay_returncode"] == 2
    assert durable["retained_publications"] == ["reserved.json", "reserved-head.json", "intent.json", "intent-head.json", "consumed.json", "consumed-head.json", "receipt.json", "receipt-head.json"]
    assert durable["epoch_sha256_before_resume"] == durable["epoch_sha256_after_refusal"] == durable["epoch_sha256_after_replay"]
    assert durable["originals_after_injection_sha256"] == value["preserved_originals_sha256"]
    assert value["preservation_baseline"] == durable["mutation"]["preservation_baseline"] == "after-deliberate-fixture-mutation"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("prepare")
    create.add_argument("--source", type=Path, required=True)
    create.add_argument("--destination", type=Path, required=True)
    create.add_argument("--evidence", type=Path, required=True)
    run = commands.add_parser("run")
    run.add_argument("--evidence", type=Path, required=True)
    run.add_argument("arguments", nargs=argparse.REMAINDER)
    validate = commands.add_parser("validate")
    validate.add_argument("--evidence", type=Path, required=True)
    validate.add_argument("--result", type=Path, required=True)
    args = parser.parse_args(argv)
    logger.remove()
    logger.add(sys.stdout, colorize=True)
    logger.add(str(args.evidence) + ".log", rotation="10 MB")
    if args.command == "prepare":
        prepare(args.source, args.destination, args.evidence)
    elif args.command == "validate":
        validate_control_result(json.loads(args.result.read_bytes()), args.evidence)
    else:
        verify_source_evidence(args.evidence, executing=True)
        from scripts.test_management_recovery_epoch_host import main as run_fixture

        arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
        return run_fixture([*arguments, "--mutation-control-manifest", str(args.evidence)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
