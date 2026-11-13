"""Shared Docker inspection canonicalization and bounded candidate discovery."""

from __future__ import annotations

import json
import os
import re


def canonical_mounts(value: object) -> list[dict[str, object]]:
    """Keep every complete mount record, including duplicates and extra fields."""
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("Docker mount inspection is malformed")
    return sorted(value, key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))


def canonical_inspection(value: dict[str, object]) -> dict[str, object]:
    """Preserve all other fields and array ordering in the full inspection."""
    return {**value, "Mounts": canonical_mounts(value.get("Mounts"))}


def canonical_legacy_inspection(value: list[object]) -> list[object]:
    """Normalize the mount field in the existing 19-field host readback."""
    if len(value) != 19:
        raise ValueError("legacy Docker inspection fields are incomplete")
    return [*value[:11], canonical_mounts(value[11]), *value[12:]]


def overlapping_management_source(candidate_mounts: object, management_sources: tuple[str, ...]) -> bool:
    if not isinstance(candidate_mounts, list):
        raise ValueError("Docker candidate mount inventory is unavailable")
    for mount in candidate_mounts:
        if not isinstance(mount, dict) or not isinstance(mount.get("Type"), str):
            raise ValueError("Docker candidate mount inventory is ambiguous")
        if mount["Type"] != "bind":
            continue
        source = mount.get("Source")
        if not isinstance(source, str) or not os.path.isabs(source):
            raise ValueError("Docker candidate bind source is ambiguous")
        source_path = os.path.normpath(source)
        if any(os.path.commonpath((source_path, os.path.normpath(original))) in (source_path, os.path.normpath(original)) for original in management_sources):
            return True
    return False


def management_command_candidate(arguments: tuple[str, ...]) -> bool:
    """Recognize a management invocation, including embedded shell commands."""
    command_text = " ".join(arguments)
    if re.search(r"(?<![\w.])auto_coder\.management\.", command_text):
        return True
    entrypoint = re.search(r"(?<![\w.-])(?:auto_coder\.cli|auto-coder)(?![\w.-])", command_text)
    return bool(entrypoint and re.search(r"\bmanagement\b", command_text) and re.search(r"\bserve\b", command_text))


def management_candidate_roles(obj: dict[str, object], container_id: str, management_id: str, proxy_id: str, management_image: object, proxy_image: object, management_sources: tuple[str, ...]) -> tuple[str, ...]:
    """Discover bounded candidates; this does not establish controller closure."""
    config, host = obj.get("Config"), obj.get("HostConfig")
    if not isinstance(config, dict) or not isinstance(host, dict):
        raise ValueError("Docker effective command or network configuration unavailable")
    command = config.get("Cmd")
    entrypoint = config.get("Entrypoint")
    if command is not None and (not isinstance(command, list) or len(command) > 64 or any(not isinstance(part, str) or len(part) > 4096 for part in command)):
        raise ValueError("Docker effective command is ambiguous")
    if entrypoint is not None and (not isinstance(entrypoint, list) or len(entrypoint) > 64 or any(not isinstance(part, str) or len(part) > 4096 for part in entrypoint)):
        raise ValueError("Docker entrypoint is ambiguous")
    network_mode = host.get("NetworkMode")
    if network_mode is not None and (not isinstance(network_mode, str) or len(network_mode) > 256):
        raise ValueError("Docker network mode is ambiguous")
    arguments = tuple((entrypoint or []) + (command or []))
    roles = []
    if container_id == management_id:
        roles.append("selected-management")
    elif obj.get("Image") == management_image:
        roles.append("same-management-image")
    if container_id == proxy_id:
        roles.append("selected-proxy")
    elif obj.get("Image") == proxy_image:
        roles.append("same-proxy-image")
    # Include shell-wrapped management invocations, not just argv tokens.
    if management_command_candidate(arguments):
        roles.append("management-command")
    if network_mode == "container:" + management_id:
        roles.append("selected-management-network")
    if container_id not in (management_id, proxy_id) and overlapping_management_source(obj.get("Mounts"), management_sources):
        roles.append("selected-management-mount-source")
    return tuple(roles)


def unrelated_inspection(obj: dict[str, object]) -> dict[str, object]:
    """Normalize only validated volatile exec IDs after independent classification."""
    if "ExecIDs" not in obj:
        return obj
    exec_ids = obj["ExecIDs"]
    if exec_ids is not None and (not isinstance(exec_ids, list) or any(not isinstance(entry, str) or not re.fullmatch(r"[0-9a-f]{64}", entry) for entry in exec_ids)):
        raise ValueError("unrelated Docker exec inventory is malformed")
    normalized = dict(obj)
    normalized["ExecIDs"] = None
    return normalized


def validate_container_facts(obj: dict[str, object]) -> tuple[str, dict, dict, list, dict, int]:
    """Require unambiguous image, restart and lifetime facts."""
    image = obj.get("Image")
    if not isinstance(image, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("Docker image identity unavailable")
    state, host, mounts = obj.get("State"), obj.get("HostConfig"), obj.get("Mounts")
    if not isinstance(state, dict) or not isinstance(host, dict) or not isinstance(mounts, list):
        raise ValueError("Docker state, host configuration or mounts unavailable")
    policy = host.get("RestartPolicy")
    if not isinstance(policy, dict) or policy.get("Name") not in ("no", "always", "unless-stopped", "on-failure") or type(policy.get("MaximumRetryCount")) is not int or policy["MaximumRetryCount"] < 0:
        raise ValueError("Docker restart policy unavailable or unsupported")
    if type(state.get("Running")) is not bool or type(state.get("Pid")) is not int or state["Pid"] < 0 or state.get("Status") not in ("created", "running", "paused", "restarting", "removing", "exited", "dead"):
        raise ValueError("Docker lifetime state unavailable or unsupported")
    if (state["Pid"] > 0) != state["Running"]:
        raise ValueError("Docker running/PID relationship is contradictory")
    if (state["Status"] in ("running", "paused") and not state["Running"]) or (state["Status"] in ("created", "exited", "dead") and state["Running"]):
        raise ValueError("Docker status and running lifetime are contradictory")
    # Docker reports these status flags independently. Never accept a
    # contradictory present flag as evidence of an unrelated worker.
    for flag, status in (("Paused", "paused"), ("Restarting", "restarting"), ("Dead", "dead")):
        if flag in state and (type(state[flag]) is not bool or state[flag] != (state["Status"] == status)):
            raise ValueError("Docker status and lifetime flag are contradictory")
    restart_count = obj.get("RestartCount")
    if any(not isinstance(state.get(key), str) or not state[key] or len(state[key]) > 128 for key in ("StartedAt", "FinishedAt")) or type(restart_count) is not int or restart_count < 0:
        raise ValueError("Docker lifetime identity unavailable")
    return image, state, host, mounts, policy, restart_count
