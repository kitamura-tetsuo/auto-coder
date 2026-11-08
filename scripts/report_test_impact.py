"""Retrospective, non-gating comparison of two exact test-impact observations.

This command never supplies a selector to pytest. Baseline edges are frozen
before inspecting current native outcomes, and no edge is proof of independence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from loguru import logger
from scripts.summarize_test_impact import PHASE_CONTEXTS, SCHEMA_VERSION

from tests.ci_impact_plugin import inventory_digest

WORKFLOW_PATH = ".github/workflows/test-impact-observation.yml"
PROFILE = "ordinary-pr-full-v1"
SELECTOR = "not browser and not opencode_live and not muse_live and not supervisor_integration"
REQUIRED_DIGESTS = (
    "pyproject.toml",
    "uv.lock",
    "tests/ci_impact_plugin.py",
    "scripts/summarize_test_impact.py",
    "scripts/run_test_impact_observation.py",
)
REQUIRED_VERSIONS = ("python", "pytest", "pytest_cov", "coverage", "uv")
SHA = re.compile(r"[0-9a-f]{40}\Z")


@dataclass(frozen=True)
class PathChange:
    status: str
    path: str
    old_path: str | None = None

    def as_json(self) -> dict[str, object]:
        return {"status": self.status, "path": self.path, "old_path": self.old_path}


class EvidenceError(ValueError):
    """Missing, ambiguous or contradictory evidence; never an empty selection."""


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise EvidenceError("expected JSON object")
    return value


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise EvidenceError("expected JSON array")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise EvidenceError("missing nonempty string")
    return value


def _identity(artifact: dict[str, object]) -> dict[str, object]:
    return _object(artifact.get("identity"))


def _inventory(artifact: dict[str, object]) -> list[str]:
    """An incomplete native run may still have a trustworthy full collection."""
    profile = _object(artifact.get("profile"))
    if profile.get("name") != PROFILE or profile.get("selector") != SELECTOR:
        raise EvidenceError("ordinary profile/selector is incompatible")
    nodes = _list(profile.get("ordered_nodeids"))
    if not nodes or not all(isinstance(node, str) and node for node in nodes):
        raise EvidenceError("complete current collection is unavailable")
    if len(nodes) != len(set(nodes)) or profile.get("node_inventory_digest") != inventory_digest(nodes):
        raise EvidenceError("collection is duplicated or its digest differs")
    return nodes


def _nodes(artifact: dict[str, object], inventory: list[str]) -> dict[str, dict[str, object]]:
    records = [_object(item) for item in _list(artifact.get("nodes"))]
    if [item.get("nodeid") for item in records] != inventory:
        raise EvidenceError("native node records do not match exact ordered collection")
    for node in records:
        phases = _object(node.get("phases"))
        if set(phases) != set(PHASE_CONTEXTS):
            raise EvidenceError(f"incomplete native phase inventory: {node['nodeid']}")
        for name, suffix in PHASE_CONTEXTS.items():
            phase = _object(phases[name])
            coverage = _object(phase.get("coverage"))
            if coverage.get("context") != f"{node['nodeid']}|{suffix}":
                raise EvidenceError(f"invalid phase coverage identity: {node['nodeid']} {name}")
            files = _list(coverage.get("files"))
            if any(not isinstance(path, str) or not path for path in files):
                raise EvidenceError(f"invalid phase paths: {node['nodeid']} {name}")
            if phase.get("state") not in {"reported", "unreached", "interrupted_or_missing_report"}:
                raise EvidenceError(f"invalid native phase state: {node['nodeid']} {name}")
            if coverage.get("state") not in {"measured", "measured_empty", "no_evidence", "unreached"}:
                raise EvidenceError(f"invalid phase mapping state: {node['nodeid']} {name}")
    return {str(node["nodeid"]): node for node in records}


def _read_artifact(path: Path) -> dict[str, object]:
    try:
        return _object(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, UnicodeError) as exc:
        raise EvidenceError(f"observation unreadable: {type(exc).__name__}: {exc}") from exc


def _git(repository_dir: Path, *args: str) -> bytes:
    try:
        return subprocess.run(["git", "-C", str(repository_dir), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise EvidenceError(f"Git object/interval unavailable for {args[0]}") from exc


def parse_name_status(data: bytes) -> list[PathChange]:
    """Parse the entire NUL-delimited Git interval, including both rename paths."""
    if not data:
        return []  # A verified empty diff is valid, unlike missing Git evidence.
    if not data.endswith(b"\0"):
        raise EvidenceError("truncated NUL-delimited Git path-status interval")
    try:
        fields = [part.decode("utf-8", errors="strict") for part in data[:-1].split(b"\0")]
    except UnicodeDecodeError as exc:
        raise EvidenceError("Git interval contains an undecodable path") from exc
    result: list[PathChange] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        if not re.fullmatch(r"(?:[AMDUTBX]|[RC][0-9]{1,3})", status):
            raise EvidenceError(f"unknown Git path-status record: {status!r}")
        count = 2 if status.startswith(("R", "C")) else 1
        if index + count > len(fields) or any(not item for item in fields[index : index + count]):
            raise EvidenceError("incomplete Git path-status record")
        if count == 2:
            result.append(PathChange(status, fields[index + 1], fields[index]))
        else:
            result.append(PathChange(status, fields[index]))
        index += count
    return sorted(result, key=lambda item: (item.path, item.status, item.old_path or ""))


def _exact_interval(repository_dir: Path, baseline: dict[str, object], current: dict[str, object]) -> list[PathChange]:
    before, after = _identity(baseline), _identity(current)
    older, newer = _string(before.get("checkout_sha")), _string(after.get("checkout_sha"))
    if not SHA.fullmatch(older) or not SHA.fullmatch(newer):
        raise EvidenceError("invalid evaluated checkout SHA")
    head = _git(repository_dir, "rev-parse", "HEAD").decode("ascii").strip()
    if head != newer:
        raise EvidenceError("current checkout differs from observed evaluated checkout")
    for identity, sha in ((before, older), (after, newer)):
        tree = _string(identity.get("checkout_tree"))
        if not SHA.fullmatch(tree) or _git(repository_dir, "rev-parse", f"{sha}^{{tree}}").decode("ascii").strip() != tree:
            raise EvidenceError(f"evaluated tree identity mismatch: {sha}")
    try:
        ancestry = subprocess.run(
            ["git", "-C", str(repository_dir), "merge-base", "--is-ancestor", older, newer],
            capture_output=True,
        )
    except OSError as exc:
        raise EvidenceError("Git ancestry command unavailable") from exc
    if ancestry.returncode != 0:
        raise EvidenceError("baseline is not a proven ancestor (or Git ancestry is unavailable)")
    return parse_name_status(_git(repository_dir, "diff", "--no-ext-diff", "--name-status", "-z", "-M", older, newer, "--"))


def _compatible(baseline: dict[str, object], current: dict[str, object], repository: str | None, run_id: int | None, attempt: int | None) -> None:
    if baseline.get("schema_version") != SCHEMA_VERSION or current.get("schema_version") != SCHEMA_VERSION:
        raise EvidenceError("observation schema version incompatible")
    if baseline.get("status") != "available" or baseline.get("errors") != []:
        raise EvidenceError("baseline observation unavailable/incomplete/corrupt")
    if current.get("status") != "available" or current.get("errors") != []:
        raise EvidenceError("current native observation unavailable/incomplete/corrupt")
    before, after = _identity(baseline), _identity(current)
    if before.get("repository") != after.get("repository") or (repository and before.get("repository") != repository):
        raise EvidenceError("foreign or mismatched observation repository")
    for identity in (before, after):
        if identity.get("workflow_path") != WORKFLOW_PATH or identity.get("workflow_event") != "workflow_dispatch":
            raise EvidenceError("wrong observation workflow provenance")
        if not _string(identity.get("invocation_id")) or not _string(identity.get("source_ref")):
            raise EvidenceError("invocation/source reference unavailable")
        if int(_string(identity.get("run_id"))) <= 0 or int(_string(identity.get("run_attempt"))) <= 0:
            raise EvidenceError("invalid run/attempt identity")
    if run_id is not None and str(run_id) != str(before.get("run_id")):
        raise EvidenceError("baseline artifact belongs to another run")
    if attempt is not None and str(attempt) != str(before.get("run_attempt")):
        raise EvidenceError("baseline artifact belongs to another attempt")
    native = _object(baseline.get("native_session"))
    if native.get("exit_code") != 0 or native.get("pytest_finalization_exit") != 0 or native.get("started") is not True or native.get("finalized") is not True:
        raise EvidenceError("baseline did not complete with a successful native pytest outcome")
    before_tooling, after_tooling = _object(baseline.get("tooling")), _object(current.get("tooling"))
    for field, required in (("versions", REQUIRED_VERSIONS), ("digests", REQUIRED_DIGESTS)):
        left, right = _object(before_tooling.get(field)), _object(after_tooling.get(field))
        if any(not left.get(key) or not right.get(key) for key in required):
            raise EvidenceError(f"{field} measurement configuration incomplete")
        if left != right:
            raise EvidenceError(f"{field} measurement configuration differs")
    _inventory(baseline)
    _inventory(current)


def _baseline_mappings(baseline: dict[str, object], node_records: dict[str, dict[str, object]]) -> tuple[dict[str, set[str]], set[str], set[str]]:
    """Trust only the old phase evidence; never read current file-to-node edges."""
    sources = _list(baseline.get("sources"))
    source_paths: set[str] = set()
    context_records: dict[str, set[str]] = {}
    for raw in sources:
        source = _object(raw)
        path = _string(source.get("path"))
        if path in source_paths or not SHA256.fullmatch(_string(source.get("sha256"))):
            raise EvidenceError("duplicate or invalid baseline source fingerprint")
        source_paths.add(path)
        contexts = _list(source.get("contexts"))
        if any(not isinstance(context, str) for context in contexts) or len(contexts) != len(set(contexts)):
            raise EvidenceError(f"source contexts invalid: {path}")
        context_records[path] = set(contexts)
    source_nodes: dict[str, set[str]] = {path: set() for path in source_paths}
    expected_contexts: dict[str, set[str]] = {path: set() for path in source_paths}
    unmapped_nodes: set[str] = set()
    for nodeid, node in node_records.items():
        phases = _object(node["phases"])
        files: set[str] = set()
        usable = True
        for phase_name in PHASE_CONTEXTS:
            phase = _object(phases[phase_name])
            coverage = _object(phase["coverage"])
            state, mapping = phase["state"], coverage["state"]
            if state == "reported":
                if phase.get("outcome") not in {"passed", "failed", "skipped"}:
                    usable = False
                if mapping not in {"measured", "measured_empty"}:
                    usable = False
            elif state == "unreached":
                if mapping != "unreached":
                    usable = False
            else:
                usable = False
            phase_files = _list(coverage["files"])
            if (mapping == "measured") != bool(phase_files):
                usable = False
            for path in phase_files:
                if isinstance(path, str) and path in expected_contexts:
                    expected_contexts[path].add(_string(coverage["context"]))
            files.update(phase_files)
        if set(_list(node.get("observed_files"))) != files or any(path not in source_nodes for path in files):
            usable = False
        if not usable:
            unmapped_nodes.add(nodeid)
        for path in files:
            if path in source_nodes and usable:
                source_nodes[path].add(nodeid)
    unattributed = _object(baseline.get("unattributed_execution"))
    if unattributed.get("state") not in {"measured", "measured_empty", "no_evidence"}:
        raise EvidenceError("unknown baseline unattributed execution state")
    unattributed_files = set(_list(unattributed.get("files")))
    if any(not isinstance(path, str) for path in unattributed_files):
        raise EvidenceError("unattributed execution paths invalid")
    if (unattributed.get("state") == "measured") != bool(unattributed_files):
        raise EvidenceError("inconsistent unattributed execution mapping")
    if any(path not in source_paths for path in unattributed_files):
        raise EvidenceError("unattributed source identity missing")
    for path in unattributed_files:
        expected_contexts[path].add("")
    if context_records != expected_contexts:
        raise EvidenceError("baseline source contexts conflict with native phase/unattributed edges")
    _object(baseline.get("unmeasured_domains"))
    return source_nodes, unmapped_nodes, unattributed_files


SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _is_test_file(path: str) -> bool:
    parts = Path(path).parts
    return len(parts) >= 2 and parts[0] == "tests" and parts[-1].startswith("test_") and parts[-1].endswith(".py")


def _selection(
    changes: list[PathChange],
    baseline: dict[str, object],
    baseline_nodes: dict[str, dict[str, object]],
    current_ids: list[str],
    repository_dir: Path,
) -> tuple[set[str], list[str], list[str], set[str]]:
    source_nodes, unmapped, unattributed = _baseline_mappings(baseline, baseline_nodes)
    before_sha = _string(_identity(baseline).get("checkout_sha"))
    current_set = set(current_ids)
    candidate = set(unmapped) & current_set
    new_nodes = sorted(current_set - baseline_nodes.keys())
    candidate.update(new_nodes)
    reasons: list[str] = []
    unmeasured_changes: set[str] = set()
    for change in changes:
        path = change.path
        if path.startswith("src/auto_coder/") and path.endswith(".py"):
            if change.status != "M":
                reasons.append(f"source_path_status:{change.status}:{path}")
                unmeasured_changes.add(path)
                continue
            if path in unattributed:
                reasons.append(f"source_has_unattributed_execution:{path}")
                unmeasured_changes.add(path)
                continue
            if path not in source_nodes or not source_nodes[path]:
                reasons.append(f"source_unmapped:{path}")
                unmeasured_changes.add(path)
                continue
            try:
                expected = next(_object(raw).get("sha256") for raw in _list(baseline["sources"]) if _object(raw).get("path") == path)
                recorded = hashlib.sha256(_git(repository_dir, "show", f"{before_sha}:{path}")).hexdigest()
            except (StopIteration, EvidenceError) as exc:
                raise EvidenceError(f"source Git fingerprint unavailable: {path}") from exc
            if expected != recorded:
                reasons.append(f"source_fingerprint_mismatch:{path}")
                unmeasured_changes.add(path)
                continue
            candidate.update(source_nodes[path] & current_set)
        elif _is_test_file(path) and (change.old_path is None or _is_test_file(change.old_path)) and (change.status in {"A", "M", "D"} or change.status.startswith(("R", "C"))):
            candidate.update(node for node in current_ids if node.split("::", 1)[0] == path)
        else:
            reasons.append(f"unmeasured_or_global_change:{change.status}:{change.old_path or ''}:{path}")
            unmeasured_changes.add(path)
    return candidate, sorted(set(reasons)), new_nodes, unmeasured_changes


def _native_overlay(current: dict[str, object], records: dict[str, dict[str, object]], omitted: list[str]) -> tuple[str, list[dict[str, object]], dict[str, object], list[dict[str, object]]]:
    unavailable_time = {"basis": "native phase sums, not CI wall/queue time", "full_ms": None, "selected_ms": None, "omitted_ms": None}
    native = _object(current.get("native_session"))
    if native.get("started") is not True or native.get("finalized") is not True or not isinstance(native.get("exit_code"), int) or native.get("exit_code") != native.get("pytest_finalization_exit"):
        return "not_validated", [], unavailable_time, []
    omissions = set(omitted)
    misses: list[dict[str, object]] = []
    native_outcomes: list[dict[str, object]] = []
    full_ms = 0.0
    omitted_ms = 0.0
    for nodeid, node in records.items():
        phase_durations = 0.0
        failures: list[str] = []
        node_outcomes: list[dict[str, object]] = []
        for name in PHASE_CONTEXTS:
            phase = _object(_object(node["phases"])[name])
            if phase["state"] == "interrupted_or_missing_report":
                return "not_validated", [], unavailable_time, []
            node_outcomes.append({"phase": name, "state": phase["state"], "outcome": phase.get("outcome"), "wasxfail": phase.get("wasxfail")})
            if phase["state"] == "reported":
                duration = phase.get("native_duration_ms")
                if not isinstance(duration, (int, float)) or duration < 0 or phase.get("outcome") not in {"passed", "failed", "skipped"}:
                    return "not_validated", [], unavailable_time, []
                phase_durations += float(duration)
                if phase["outcome"] == "failed":
                    failures.append(name)
        if _object(_object(node["phases"])["setup"])["state"] == "unreached" or _object(_object(node["phases"])["teardown"])["state"] == "unreached":
            return "not_validated", [], unavailable_time, []
        full_ms += phase_durations
        if nodeid in omissions:
            omitted_ms += phase_durations
            native_outcomes.append({"nodeid": nodeid, "phases": node_outcomes})
            if failures:
                misses.append({"nodeid": nodeid, "failed_phases": failures, "classification": "MISSED_FAILURE"})
    return (
        "missed_failure" if misses else "no_observed_miss",
        sorted(misses, key=lambda record: str(record["nodeid"])),
        {"basis": "sum of full current native pytest phase times; NOT CI wall/queue time", "full_ms": round(full_ms, 3), "selected_ms": round(full_ms - omitted_ms, 3), "omitted_ms": round(omitted_ms, 3)},
        sorted(native_outcomes, key=lambda record: str(record["nodeid"])),
    )


def _result(
    status: str,
    mode: str,
    reasons: list[str],
    current: dict[str, object] | None,
    baseline: dict[str, object] | None,
    inventory: list[str] | None,
    changes: list[PathChange] | None = None,
    candidate: set[str] | None = None,
    new_nodes: list[str] | None = None,
    removed_nodes: list[str] | None = None,
    unmeasured: set[str] | None = None,
    misses: list[dict[str, object]] | None = None,
    validation: str = "not_validated",
    timing: dict[str, object] | None = None,
    omitted_outcomes: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    selected = sorted(candidate if candidate is not None else inventory or []) if mode != "UNKNOWN" else []
    omitted = sorted(set(inventory or []) - set(selected)) if status == "candidate" else []
    return {
        "schema_version": 1,
        "report_kind": "retrospective_test_impact_shadow",
        "advisory_only": True,
        "empirical_not_soundness_guarantee": True,
        "comparison_status": status,
        "selection_mode": mode,
        "reasons": sorted(set(reasons)),
        "baseline_identity": baseline.get("identity") if baseline else None,
        "current_identity": current.get("identity") if current else None,
        "profile": PROFILE,
        "selector": SELECTOR,
        "collection_known": inventory is not None,
        "complete_profile_required": True,
        "compared_interval": {
            "baseline_checkout_sha": _identity(baseline).get("checkout_sha") if baseline and isinstance(baseline.get("identity"), dict) else None,
            "current_checkout_sha": _identity(current).get("checkout_sha") if current and isinstance(current.get("identity"), dict) else None,
            "verified_ancestry_and_full_path_status": changes is not None,
        },
        "changed_paths": [change.as_json() for change in changes] if changes is not None else None,
        "candidate_nodes": selected,
        "omitted_nodes": omitted,
        "new_nodes": new_nodes or [],
        "removed_nodes": removed_nodes or [],
        "unmeasured_changed_paths": sorted(unmeasured or []),
        "unmeasured_domains": baseline.get("unmeasured_domains") if baseline else None,
        "validation_status": validation,
        "missed_failures": misses or [],
        "omitted_native_outcomes": omitted_outcomes or [],
        "phase_time_estimate": timing,
        "executed_profile": "full ordinary PR pytest profile; candidate selection was never applied",
    }


def compare(
    baseline: dict[str, object] | None,
    current: dict[str, object] | None,
    repository_dir: Path,
    repository: str | None = None,
    run_id: int | None = None,
    attempt: int | None = None,
    external_errors: list[str] | None = None,
) -> dict[str, object]:
    """Compute candidate only from baseline, then overlay actual current failures."""
    reasons = list(external_errors or [])
    inventory: list[str] | None = None
    if current is not None:
        try:
            inventory = _inventory(current)
        except EvidenceError as exc:
            reasons.append(f"current_collection_unavailable:{exc}")
    if baseline is None:
        reasons.append("baseline_artifact_unavailable")
    if current is None:
        reasons.append("current_artifact_unavailable")
    if reasons:
        return _result("unavailable", "FULL" if inventory is not None else "UNKNOWN", reasons, current, baseline, inventory)
    assert baseline is not None and current is not None and inventory is not None
    try:
        _compatible(baseline, current, repository, run_id, attempt)
        baseline_ids = _inventory(baseline)
        baseline_nodes = _nodes(baseline, baseline_ids)
        current_nodes = _nodes(current, inventory)
        changes = _exact_interval(repository_dir, baseline, current)
        candidate, full_reasons, new_nodes, unmeasured = _selection(changes, baseline, baseline_nodes, inventory, repository_dir)
        removed = sorted(set(baseline_ids) - set(inventory))
        if full_reasons:
            return _result("full", "FULL", full_reasons, current, baseline, inventory, changes, new_nodes=new_nodes, removed_nodes=removed, unmeasured=unmeasured)
        omitted = sorted(set(inventory) - candidate)
        validation, misses, timing, omitted_outcomes = _native_overlay(current, current_nodes, omitted)
        return _result("candidate", "BASELINE_CANDIDATE", [], current, baseline, inventory, changes, candidate, new_nodes, removed, unmeasured, misses, validation, timing, omitted_outcomes)
    except (EvidenceError, ValueError, TypeError, KeyError) as exc:
        return _result("unavailable", "FULL", [f"evidence_or_git_unavailable:{exc}"], current, baseline, inventory)


def _baseline_path(directory: Path) -> Path:
    """Accept one exact artifact, including download-action's one-match flattening."""
    matches = sorted(directory.glob("*/observation.json")) if directory.is_dir() else []
    if (directory / "observation.json").is_file():
        matches.append(directory / "observation.json")
    if len(matches) != 1:
        raise EvidenceError(f"expected exactly one downloaded attempt artifact, found {len(matches)}")
    return matches[0]


def _validate_downloaded_artifact(path: Path, baseline: dict[str, object]) -> None:
    """A downloaded JSON alone cannot stand in for F's complete raw artifact."""
    from coverage import CoverageData

    raw_dir = path.parent
    files = ("manifest.json", "native-exit.json", "coverage.db")
    if any(not (raw_dir / name).is_file() for name in files):
        raise EvidenceError("downloaded baseline lacks raw F observation receipts/coverage")
    events = sorted(raw_dir.glob("pytest-events-*.jsonl"))
    if len(events) != 1:
        raise EvidenceError("downloaded baseline has missing/ambiguous F native event log")
    manifest, receipt = _read_artifact(raw_dir / "manifest.json"), _read_artifact(raw_dir / "native-exit.json")
    identity = _identity(baseline)
    for key in ("repository", "checkout_sha", "checkout_tree", "workflow_path", "workflow_event", "run_id", "run_attempt", "invocation_id"):
        if manifest.get(key) != identity.get(key):
            raise EvidenceError(f"downloaded manifest/normalized identity conflict: {key}")
    if receipt.get("invocation_id") != identity.get("invocation_id") or receipt.get("exit_code") != _object(baseline.get("native_session")).get("exit_code"):
        raise EvidenceError("downloaded native exit receipt conflicts with normalized result")
    try:
        coverage = CoverageData(basename=str(raw_dir / "coverage.db"))
        coverage.read()
        if not coverage.measured_files() or set(coverage.measured_contexts()) != set(_list(baseline.get("observed_contexts"))):
            raise EvidenceError("downloaded coverage contexts conflict with normalized observation")
        raw_events = [json.loads(line) for line in events[0].read_text(encoding="utf-8").splitlines()]
        if any(not isinstance(item, dict) or item.get("invocation_id") != identity.get("invocation_id") for item in raw_events):
            raise EvidenceError("downloaded event log contains an unexpected invocation")
        collections = [item for item in raw_events if item.get("kind") == "collection"]
        if len(collections) != 1 or collections[0].get("nodeids") != _inventory(baseline):
            raise EvidenceError("downloaded native collection disagrees with normalized inventory")
        if len([item for item in raw_events if item.get("kind") == "session_finish"]) != 1:
            raise EvidenceError("downloaded native session finalization unavailable")
    except Exception as exc:
        if isinstance(exc, EvidenceError):
            raise
        raise EvidenceError(f"downloaded raw F evidence corrupt: {type(exc).__name__}: {exc}") from exc


def _verify_remote_attempt(repository: str, run_id: int, attempt: int, token: str) -> None:
    """Check GitHub's authoritative per-attempt state via the read-only REST boundary."""
    from auto_coder.util.gh_cache import GitHubGitDataClient

    response = GitHubGitDataClient(token, repository).request("GET", f"actions/runs/{run_id}/attempts/{attempt}")
    if response.status != 200:
        raise EvidenceError(f"baseline workflow attempt API unavailable: HTTP {response.status}")
    data = _object(response.data)
    source_repo = _object(data.get("repository"))
    workflow_path = data.get("path")
    if (
        str(data.get("id")) != str(run_id)
        or str(data.get("run_attempt")) != str(attempt)
        or source_repo.get("full_name") != repository
        or not isinstance(workflow_path, str)
        or workflow_path.split("@", 1)[0] != WORKFLOW_PATH
        or data.get("event") != "workflow_dispatch"
        or data.get("status") != "completed"
        or data.get("conclusion") != "success"
    ):
        raise EvidenceError("baseline GitHub attempt is foreign, wrong workflow, incomplete or not successful")


def markdown_summary(report: dict[str, object]) -> str:
    def code(value: object) -> str:
        return "`" + str(value).replace("`", "\\`").replace("\n", " ") + "`"

    def identity_line(name: str) -> str:
        identity = report.get(name)
        if not isinstance(identity, dict):
            return f"- {name}: unavailable"
        return f"- {name}: repo {code(identity.get('repository'))}, run/attempt " f"{code(identity.get('run_id'))}/{code(identity.get('run_attempt'))}, " f"invocation {code(identity.get('invocation_id'))}, tree {code(identity.get('checkout_tree'))}"

    interval = _object(report["compared_interval"])
    lines = [
        "## Retrospective test-impact shadow report (non-gating)",
        "",
        "This report never selects or skips tests. All ordinary-profile tests were invoked. Baseline execution edges are empirical, not a soundness guarantee.",
        "",
        f"- Comparison: **{report['comparison_status']}**; mode: **{report['selection_mode']}**",
        f"- Validation: **{report['validation_status']}**",
        f"- Baseline evaluated checkout: {code(interval['baseline_checkout_sha'])}",
        f"- Current evaluated checkout: {code(interval['current_checkout_sha'])}",
        identity_line("baseline_identity"),
        identity_line("current_identity"),
        f"- Profile/marker selector: {code(report['profile'])} / {code(report['selector'])}",
        f"- Exact ancestry/full interval proven: {code(interval['verified_ancestry_and_full_path_status'])}",
        f"- Current collected inventory known: {code(report['collection_known'])}",
        f"- Hypothetical selected/omitted/new/removed nodes: **{len(report['candidate_nodes'])}/{len(report['omitted_nodes'])}/{len(report['new_nodes'])}/{len(report['removed_nodes'])}**",
        "",
    ]
    if report["reasons"]:
        lines.extend(["### Conservative fallback reasons", "", *[f"- {code(reason)}" for reason in report["reasons"]], ""])
    if report["missed_failures"]:
        lines.extend(["### MISSED_FAILURE (real current native failures hypothetically omitted)", "", *[f"- {code(_object(record)['nodeid'])}" for record in report["missed_failures"]], ""])
    timing = report.get("phase_time_estimate")
    if isinstance(timing, dict) and timing.get("full_ms") is not None:
        lines.extend(["### Retrospective native phase-time sums (ms)", "", f"- Full: {timing['full_ms']}; selected: {timing['selected_ms']}; omitted: {timing['omitted_ms']}", "- These sums exclude CI queue, startup, scheduling and wall time.", ""])
    if report["changed_paths"] is not None:
        lines.extend(["### Complete baseline-to-current path-status interval", "", *[f"- {code(_object(change)['status'])} {code(_object(change)['old_path']) if _object(change)['old_path'] else ''} {code(_object(change)['path'])}" for change in report["changed_paths"]], ""])
    for key, title in (
        ("candidate_nodes", "Hypothetical candidate nodes (full profile still executed)"),
        ("omitted_nodes", "Hypothetically omitted nodes (all actually executed)"),
        ("new_nodes", "Newly collected nodes"),
        ("removed_nodes", "Removed historical nodes"),
        ("unmeasured_changed_paths", "Changed unmeasured-domain paths"),
    ):
        values = _list(report[key])
        lines.extend([f"### {title}: {len(values)}", ""])
        lines.extend(f"- {code(node)}" for node in values[:250])
        if len(values) > 250:
            lines.append(f"- … {len(values) - 250} additional exact entries in the JSON artifact")
        if not values:
            lines.append("- None")
        lines.append("")
    domains = report.get("unmeasured_domains")
    if isinstance(domains, dict):
        lines.extend(["### Permanently unmeasured observation domains", ""])
        lines.extend(f"- {code(key)}: {str(value).replace(chr(10), ' ')}" for key, value in sorted(domains.items()))
        lines.append("")
    lines.extend(["**No test-omission, merge, promotion or repair authority is granted by this report.**", ""])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--baseline-directory", type=Path)
    parser.add_argument("--baseline-run-id")
    parser.add_argument("--baseline-run-attempt")
    parser.add_argument("--verify-baseline-run", action="store_true")
    parser.add_argument("--repository")
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--repository-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args()
    logger.remove()
    logger.add(sys.stdout, colorize=True, format="<green>{time}</green> | <level>{level}</level> | {file}:{function}:{line} | {message}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    logger.add(args.output.with_suffix(".log"), rotation="10 MB", format="{time} | {level} | {file}:{function}:{line} | {message}")
    errors: list[str] = []
    baseline = None
    current = None

    def positive_identity(value: str | None, label: str) -> int | None:
        if value is None:
            return None
        if not value.isascii() or not value.isdecimal() or len(value) > 20 or int(value) <= 0:
            errors.append(f"invalid {label}; a positive exact integer is required")
            return None
        return int(value)

    run_id = positive_identity(args.baseline_run_id, "baseline run ID")
    attempt = positive_identity(args.baseline_run_attempt, "baseline run attempt")
    try:
        current = _read_artifact(args.current)
    except EvidenceError as exc:
        errors.append(f"current:{exc}")
    if (args.baseline_run_id is None) != (args.baseline_run_attempt is None):
        errors.append("both exact baseline run ID and attempt are required")
    if args.baseline and args.baseline_directory:
        errors.append("choose either an exact baseline file or downloaded artifact directory")
    try:
        path = args.baseline or (_baseline_path(args.baseline_directory) if args.baseline_directory else None)
        if path:
            baseline = _read_artifact(path)
            if args.baseline_directory:
                _validate_downloaded_artifact(path, baseline)
    except EvidenceError as exc:
        errors.append(f"baseline:{exc}")
    if args.verify_baseline_run:
        if not args.repository or not run_id or not attempt:
            errors.append("remote verification requires exact repository, run and attempt")
        else:
            try:
                _verify_remote_attempt(args.repository, run_id, attempt, os.environ.get("GITHUB_TOKEN", ""))
            except (EvidenceError, RuntimeError, ValueError) as exc:
                errors.append(f"remote_attempt:{exc}")
    report = compare(baseline, current, args.repository_dir, args.repository, run_id, attempt, errors)
    try:
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(report, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
        with args.summary.open("x", encoding="utf-8") as summary:
            summary.write(markdown_summary(report))
    except OSError as exc:
        logger.error("Shadow report output failure: {}", exc)
        raise SystemExit(2) from exc
    logger.info("Shadow report: {} / {} / {}", report["comparison_status"], report["selection_mode"], report["validation_status"])


if __name__ == "__main__":
    main()
