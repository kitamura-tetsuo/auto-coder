"""Normalize one immutable pytest-cov test-impact observation invocation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import uuid
from pathlib import Path

from coverage import CoverageData

from tests.ci_impact_plugin import inventory_digest

SCHEMA_VERSION = 1
PHASE_CONTEXTS = {"setup": "setup", "call": "run", "teardown": "teardown"}
UNMEASURED_DOMAINS = {
    "subprocess": "unmeasured: Python child processes are not traced by this profile",
    "docker": "unmeasured: separate container executions are not traced",
    "non_python_resources": "unmeasured: files, network and other non-Python resources are not traced",
    "environment": "unmeasured: environment/configuration reads are not traced",
    "dynamically_unobserved": "unmeasured: uncovered runtime paths cannot establish independence",
    "unsupported": "unmeasured: native code, external tools and unrecognized coverage domains",
}


def _json_file(path: Path, errors: list[str]) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("not a JSON object")
        return payload
    except (OSError, ValueError) as exc:
        errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
        return None


def _events(directory: Path, invocation: object, errors: list[str]) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    paths = sorted(directory.glob("pytest-events-*.jsonl"))
    if len(paths) != 1:
        errors.append(f"expected one invocation-owned pytest event file, found {len(paths)}")
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        for number, line in enumerate(lines, 1):
            try:
                event = json.loads(line)
                if not isinstance(event, dict) or event.get("schema_version") != SCHEMA_VERSION or event.get("invocation_id") != invocation or not isinstance(event.get("kind"), str):
                    raise ValueError("invalid schema or conflicting invocation")
                events.append(event)
            except (ValueError, TypeError) as exc:
                errors.append(f"{path.name}:{number}: {exc}")
    return events


def _only_event(events: list[dict[str, object]], kind: str, errors: list[str]) -> dict[str, object] | None:
    matches = [event for event in events if event["kind"] == kind]
    if len(matches) != 1:
        errors.append(f"expected exactly one {kind} event, found {len(matches)}")
        return None
    return matches[0]


def _measured_coverage(directory: Path, checkout: Path, inventory: set[str], errors: list[str]) -> tuple[dict[str, set[str]], dict[str, dict[str, object]], set[str]]:
    """Return exact context→repo-file mapping, source identities and contexts.

    Never infer an absent context from another context or from a test name.
    """
    contexts: dict[str, set[str]] = {}
    sources: dict[str, dict[str, object]] = {}
    measured_contexts: set[str] = set()
    path = directory / "coverage.db"
    if not path.is_file():
        errors.append("coverage.db is missing")
        return contexts, sources, measured_contexts
    try:
        data = CoverageData(basename=str(path))
        data.read()
        measured_contexts = set(data.measured_contexts())
        files = sorted(data.measured_files())
        if not files:
            errors.append("coverage database contains no measured files")
        for filename in files:
            source_path = Path(filename).resolve()
            if not source_path.is_relative_to(checkout) or source_path.suffix != ".py":
                # Coverage of other code must not become a repository source edge.
                continue
            relative = source_path.relative_to(checkout).as_posix()
            if not source_path.is_file():
                errors.append(f"measured source vanished: {relative}")
                continue
            digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
            by_line = data.contexts_by_lineno(filename)
            if not isinstance(by_line, dict):
                raise ValueError(f"invalid coverage line/context data for {relative}")
            file_contexts: set[str] = set()
            for line_contexts in by_line.values():
                file_contexts.update(line_contexts)
            sources[relative] = {"path": relative, "sha256": digest, "contexts": sorted(file_contexts)}
            for context in file_contexts:
                contexts.setdefault(context, set()).add(relative)
        for context in measured_contexts:
            if context == "":
                continue
            if "|" not in context:
                errors.append(f"unknown coverage context: {context}")
                continue
            nodeid, phase = context.rsplit("|", 1)
            if nodeid not in inventory or phase not in PHASE_CONTEXTS.values():
                errors.append(f"unknown coverage context: {context}")
    except Exception as exc:
        errors.append(f"coverage database is unreadable or corrupt: {type(exc).__name__}: {exc}")
        contexts.clear()
        sources.clear()
        measured_contexts.clear()
    return contexts, sources, measured_contexts


def _phase_records(events: list[dict[str, object]], inventory: set[str], errors: list[str]) -> dict[tuple[str, str], dict[str, dict[str, object]]]:
    phases: dict[tuple[str, str], dict[str, dict[str, object]]] = {}
    for event in events:
        kind = event["kind"]
        if kind not in {"phase_start", "phase_boundary", "phase_report"}:
            continue
        nodeid, phase = event.get("nodeid"), event.get("phase")
        if not isinstance(nodeid, str) or nodeid not in inventory or phase not in PHASE_CONTEXTS:
            errors.append(f"unknown node/phase in {kind}: {nodeid!r} {phase!r}")
            continue
        keyed = phases.setdefault((nodeid, phase), {})
        if kind in keyed:
            errors.append(f"conflicting duplicate {kind}: {nodeid} {phase}")
        else:
            keyed[kind] = event
    return phases


def normalize(directory: Path, checkout: Path | None = None) -> dict[str, object]:
    directory = directory.resolve()
    checkout = (checkout or Path.cwd()).resolve()
    errors: list[str] = []
    if not directory.is_dir():
        errors.append("invocation directory is unavailable")
    manifest = _json_file(directory / "manifest.json", errors)
    native = _json_file(directory / "native-exit.json", errors)
    invocation = manifest.get("invocation_id") if manifest else None
    if not isinstance(invocation, str) or invocation != directory.name:
        errors.append("manifest invocation does not match owned directory")
    events = _events(directory, invocation, errors)
    session_start = _only_event(events, "session_start", errors)
    collection = _only_event(events, "collection", errors)
    session_finish = _only_event(events, "session_finish", errors)

    nodeids = collection.get("nodeids") if collection else None
    if not isinstance(nodeids, list) or not nodeids or not all(isinstance(node, str) and node for node in nodeids) or len(nodeids) != len(set(nodeids)):
        errors.append("complete unique ordered collection inventory is unavailable")
        nodeids = []
    if collection and (collection.get("digest") != inventory_digest(nodeids)):
        errors.append("collection inventory digest mismatch")
    if native is None or native.get("invocation_id") != invocation or not isinstance(native.get("exit_code"), int):
        errors.append("native pytest exit receipt is unavailable or conflicts")
    if session_finish and native and session_finish.get("exit_code") != native.get("exit_code"):
        errors.append("native exit and pytest session finalization disagree")
    if manifest:
        if not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("checkout_sha", ""))) or not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("checkout_tree", ""))):
            errors.append("actual checkout commit/tree identity missing")
        if not manifest.get("repository") or not manifest.get("source_ref") or not manifest.get("workflow_path") or not manifest.get("workflow_event") or not manifest.get("run_id") or not manifest.get("run_attempt"):
            errors.append("workflow/repository/source_ref/run/attempt identity incomplete")
        if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("profile") != "ordinary-pr-full-v1" or manifest.get("selector") != "not browser and not opencode_live and not muse_live and not supervisor_integration":
            errors.append("profile/schema/selector identity mismatch")
        versions, digests = manifest.get("versions"), manifest.get("digests")
        if not isinstance(versions, dict) or not all(versions.get(key) for key in ("python", "pytest", "pytest_cov", "coverage", "uv")):
            errors.append("Python/pytest/coverage/uv version identity incomplete")
        if not isinstance(digests, dict) or not all(digests.get(key) for key in ("pyproject.toml", "uv.lock", "tests/ci_impact_plugin.py", "scripts/summarize_test_impact.py")):
            errors.append("lock/config/observer fingerprint identity incomplete")
        try:
            actual_sha = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            actual_tree = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD^{tree}"], check=True, capture_output=True, text=True).stdout.strip()
            if actual_sha != manifest.get("checkout_sha") or actual_tree != manifest.get("checkout_tree"):
                errors.append("current checkout commit/tree differs from observed invocation")
            changes = subprocess.run(["git", "-C", str(checkout), "diff", "--name-only", "HEAD"], check=True, capture_output=True, text=True).stdout.splitlines()
            if any(path.endswith(".py") for path in changes):
                errors.append("tracked Python source changed during observation; postrun file identity cannot prove executed contents")
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f"checkout provenance could not be verified: {exc}")
        if isinstance(digests, dict):
            for name in ("uv.lock", "pyproject.toml", "tests/ci_impact_plugin.py", "scripts/summarize_test_impact.py"):
                path = checkout / name
                try:
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    if digest != digests.get(name):
                        errors.append(f"postrun observer/lock/config differs from start: {name}")
                except OSError as exc:
                    errors.append(f"postrun observer/lock/config unreadable: {name}: {exc}")

    coverage_contexts, sources, measured_contexts = _measured_coverage(directory, checkout, set(nodeids), errors)
    phase_records = _phase_records(events, set(nodeids), errors)
    nodes: list[dict[str, object]] = []
    for nodeid in nodeids:
        phases: dict[str, dict[str, object]] = {}
        node_files: set[str] = set()
        for phase, suffix in PHASE_CONTEXTS.items():
            evidence = phase_records.get((nodeid, phase), {})
            start, boundary, report = (evidence.get(key) for key in ("phase_start", "phase_boundary", "phase_report"))
            context = f"{nodeid}|{suffix}"
            observed_files = sorted(coverage_contexts.get(context, set()))
            node_files.update(observed_files)
            if report:
                state = "reported"
                if start is None or boundary is None or report.get("outcome") not in {"passed", "failed", "skipped"} or not isinstance(report.get("duration_ms"), (int, float)):
                    errors.append(f"missing/invalid native phase start, boundary or outcome: {nodeid} {phase}")
            elif start is not None or boundary is not None:
                state = "interrupted_or_missing_report"
                errors.append(f"unfinished native phase: {nodeid} {phase}")
            else:
                state = "unreached"
            if state == "unreached":
                coverage_state = "unreached"
                if context in measured_contexts:
                    errors.append(f"coverage context without native phase: {context}")
            elif context not in measured_contexts:
                coverage_state = "no_evidence"
                # A native skipped phase can have no in-process execution at all.
                # Preserve no_evidence without inventing a measured-empty edge.
                if not report or report.get("outcome") != "skipped":
                    errors.append(f"no coverage context for native phase: {context}")
            else:
                coverage_state = "measured" if observed_files else "measured_empty"
            phases[phase] = {
                "state": state,
                "outcome": report.get("outcome") if report else None,
                "native_duration_ms": report.get("duration_ms") if report else None,
                "wasxfail": report.get("wasxfail") if report else None,
                "coverage": {"state": coverage_state, "context": context, "files": observed_files},
            }
        if phases["setup"]["state"] == "unreached":
            errors.append(f"collected node never reached setup: {nodeid}")
        if phases["call"]["state"] == "unreached" and phases["setup"]["outcome"] == "passed" and session_finish:
            errors.append(f"successful setup but missing call: {nodeid}")
        if phases["teardown"]["state"] == "unreached" and session_finish:
            errors.append(f"missing teardown: {nodeid}")
        nodes.append({"nodeid": nodeid, "phases": phases, "observed_files": sorted(node_files), "unmeasured_domains": list(UNMEASURED_DOMAINS)})

    # Empty-context execution includes collection/import code and code between
    # tests. It must not be assigned to any test or used to prove independence.
    unattributed_files = sorted(coverage_contexts.get("", set()))
    empty_context_state = "measured" if unattributed_files else "measured_empty" if "" in measured_contexts else "no_evidence"
    if not measured_contexts:
        errors.append("coverage database has no execution contexts")
    evidence_exists = bool(events or manifest or native)
    status = "incomplete" if errors and evidence_exists else "unavailable" if errors else "available"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "errors": sorted(set(errors)),
        "identity": {key: manifest.get(key) for key in ("repository", "source_ref", "checkout_sha", "checkout_tree", "event_sha", "workflow_path", "workflow_event", "run_id", "run_attempt", "invocation_id")} if manifest else None,
        "profile": {"name": manifest.get("profile") if manifest else None, "selector": manifest.get("selector") if manifest else None, "ordered_nodeids": nodeids, "node_inventory_digest": inventory_digest(nodeids) if nodeids else None},
        "tooling": {"versions": manifest.get("versions"), "digests": manifest.get("digests")} if manifest else None,
        "native_session": {"exit_code": native.get("exit_code") if native else None, "pytest_finalization_exit": session_finish.get("exit_code") if session_finish else None, "started": session_start is not None, "finalized": session_finish is not None},
        "nodes": nodes,
        "sources": [sources[key] for key in sorted(sources)],
        "unattributed_execution": {"state": empty_context_state, "files": unattributed_files, "scope": "collection/import and all other out-of-test execution"},
        "unmeasured_domains": UNMEASURED_DOMAINS,
        "observed_contexts": sorted(measured_contexts),
        "raw_files": sorted(path.name for path in directory.iterdir()) if directory.is_dir() else [],
    }


def write_immutable(output: Path, report: dict[str, object]) -> None:
    """Finalize atomically without replacing any earlier completed artifact."""
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--checkout", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = normalize(args.directory, args.checkout)
    try:
        write_immutable(args.output, report)
    except OSError as exc:
        print(f"Test-impact normalization output failure: {exc}", file=__import__("sys").stderr, flush=True)
        raise SystemExit(2) from exc
    print(f"Test-impact observation: {report['status']} ({len(report['errors'])} diagnostic errors)")
    raise SystemExit(0 if report["status"] == "available" else 2)


if __name__ == "__main__":
    main()
