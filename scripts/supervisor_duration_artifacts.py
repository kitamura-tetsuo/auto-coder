"""Restore optional measured profiles and update them from native shard JSONL."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from loguru import logger
from supervisor_ci_profiles import PROFILES, REPOSITORY
from supervisor_native_evidence import NativeEvidenceError, _object, _read

from auto_coder.ci_duration_profile import ProfileContext, ProfileIdentity, build_profile, write_profile
from auto_coder.ci_shard_plan import read_plan
from auto_coder.util.gh_cache import GitHubGitDataClient

LOG_FORMAT = "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level}</level> | {file}:{function}:{line} | {message}"


def find_previous(client: GitHubGitDataClient, profile: str, current_run: int) -> tuple[str, int] | None:
    """Find a prior immutable artifact; invalid history is handled by A's parser."""
    prefix = f"supervisor-duration-{profile}-"
    candidates: list[tuple[str, str, int]] = []
    for page in range(1, 4):
        response = client.request("GET", f"actions/artifacts?per_page=100&page={page}")
        if response.status != 200:
            raise NativeEvidenceError(f"duration artifact listing HTTP {response.status}")
        data = _object(response.data, "duration artifact list")
        artifacts = data.get("artifacts")
        if not isinstance(artifacts, list):
            raise NativeEvidenceError("duration artifact listing is missing")
        for item in artifacts:
            artifact = _object(item, "duration artifact")
            name = artifact.get("name")
            if not isinstance(name, str) or not name.startswith(prefix) or artifact.get("expired") is not False:
                continue
            match = re.fullmatch(re.escape(prefix) + r"([0-9]+)-([0-9]+)", name)
            origin = artifact.get("workflow_run")
            if match is None or not isinstance(origin, dict) or origin.get("id") != int(match.group(1)):
                continue
            run_id = int(match.group(1))
            if run_id == current_run:
                continue
            created = artifact.get("created_at")
            if isinstance(created, str):
                candidates.append((created, name, run_id))
        if len(artifacts) < 100:
            break
    if not candidates:
        return None
    _created, name, run_id = max(candidates)
    return name, run_id


def _update(profile: str, plan_dir: Path, artifact_dir: Path, selections: list[dict[str, object]], output_dir: Path) -> None:
    plan, _raw = read_plan(plan_dir / profile / "plan.json")
    identity = ProfileIdentity.parse(plan.profile)
    destination = output_dir / profile
    destination.mkdir(parents=True, exist_ok=True)
    context_file = destination / "duration-context.json"
    context_file.write_text(json.dumps({"schema_version": "ci-duration-context-v1", "profile": identity.json(), "evaluation_time_utc": datetime.now(timezone.utc).isoformat()}, sort_keys=True) + "\n")
    manifests = []
    for selection in selections:
        if selection.get("profile") != profile:
            continue
        shard = selection.get("shard")
        if type(shard) is not int or not 1 <= shard <= len(plan.shards):
            raise NativeEvidenceError("validated selection has invalid duration shard")
        artifact = selection.get("artifact")
        if not isinstance(artifact, str) or "/" in artifact or ".." in artifact:
            raise NativeEvidenceError("validated duration artifact path is invalid")
        timing = artifact_dir / artifact / "timing"
        paths = sorted(timing.glob("timing-*.jsonl"))
        if not paths:
            logger.warning("{} shard {} has no optional native timing JSONL", profile, shard)
            continue
        manifest = destination / f"batch-shard-{shard}.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": "ci-duration-batch-v1",
                    "profile": identity.json(),
                    "origin": {
                        "kind": "github-actions",
                        "checkout_sha": plan.context.checkout_sha,
                        "event_sha": plan.context.origin.get("event_sha"),
                        "workflow": plan.context.origin.get("workflow"),
                        "run_id": plan.context.origin.get("run_id"),
                        "run_attempt": str(selection["producing_attempt"]),
                        "job_key": profile,
                        "invocation_id": f"{profile}-shard-{shard}",
                    },
                    "files": [os.path.relpath(path, manifest.parent) for path in paths],
                },
                sort_keys=True,
                indent=2,
            )
            + "\n"
        )
        manifests.append(manifest)
    previous = plan_dir / profile / "history" / "profile.json"
    previous_files = [previous] if previous.is_file() else []
    result = build_profile(ProfileContext.read(context_file), manifests, previous_files)
    output = destination / "profile.json"
    write_profile(output, result, [context_file, *manifests, *previous_files])
    logger.info("{} native duration update: {} nodes, {} batches, status {}", profile, len(cast(dict[str, object], result.payload["nodes"])), len(manifests), result.payload["status"])
    for item in result.diagnostics:
        for reason in item.reasons:
            logger.warning("{} diagnostic: {}", profile, reason)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("find", "update"))
    parser.add_argument("--profile", choices=PROFILES)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--plans", type=Path, default=Path("evidence/plans"))
    parser.add_argument("--artifacts", type=Path, default=Path("evidence/shards"))
    parser.add_argument("--selections", type=Path, default=Path("supervisor-integration-logs/validated-selections.json"))
    parser.add_argument("--output-dir", type=Path, default=Path("supervisor-duration"))
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(sys.stdout, format=LOG_FORMAT, colorize=True)
    logger.add(args.output_dir / "supervisor-duration.log", format=LOG_FORMAT, colorize=False, rotation="10 MB", retention=5)
    try:
        if args.operation == "find":
            if args.profile is None or args.github_output is None:
                parser.error("find requires --profile and --github-output")
            found = find_previous(GitHubGitDataClient(os.environ.get("GITHUB_TOKEN", ""), REPOSITORY), args.profile, int(os.environ["GITHUB_RUN_ID"]))
            with args.github_output.open("a", encoding="utf-8") as stream:
                stream.write("name=" + (found[0] if found else "") + "\n")
                stream.write("run_id=" + (str(found[1]) if found else "") + "\n")
        else:
            loaded = json.loads(args.selections.read_text(encoding="utf-8"))
            if not isinstance(loaded, list):
                raise NativeEvidenceError("validated selection manifest is malformed")
            selected = [_object(item, "validated selection") for item in loaded]
            for profile in PROFILES:
                _update(profile, args.plans, args.artifacts, selected, args.output_dir)
    except (NativeEvidenceError, OSError, ValueError, KeyError, RuntimeError) as error:
        logger.warning("Optional Supervisor duration snapshot unavailable: {}", error)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
