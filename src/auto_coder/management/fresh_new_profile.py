"""Independent NEW-origin configuration verification without launch authority.

The operator's names in an origin request are not proof of the selected
bootstrap, executable, application, endpoint or retained OAuth authority.
This consumer verifies those selections using the production parsers but has
no authority to create stores, admit a child or recover an earlier lifetime.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .fresh_begin import inspect_new_instance_origin
from .fresh_preflight import NewInstanceRequest, _file, _unique


@dataclass(frozen=True)
class NewProfileReceipt:
    request_id: str
    origin_sha256: str
    instance_id: str
    runtime_reference: str
    listener_url: str
    oauth_project: str
    status: str = "profile-verified-only"
    launch_authorized: bool = False


def inspect_new_instance_profile(request_file: Path) -> NewProfileReceipt:
    """Reject a semantically mismatched, unsupported or changed NEW profile.

    This is a separate-process proof of configuration selection only. A
    matching runtime receipt and a well-formed retained OAuth database are
    not proof of a live process, grant, owner fence or first-launch eligibility.
    """
    from .action_scopes import READ_SCOPE
    from .bootstrap_supervisor import BootstrapConfig
    from .fresh_oauth import validate_retained_oauth
    from .oauth_config import load_management_settings

    origin = inspect_new_instance_origin(request_file)
    _, encoded = _file(request_file, private=True, limit=65536)
    selected = NewInstanceRequest(**json.loads(encoded, object_pairs_hook=_unique))
    bootstrap_path = Path(selected.bootstrap_config)
    management_path = Path(selected.management_config)
    config = BootstrapConfig.read(bootstrap_path)
    settings = load_management_settings(management_path)
    runtime = next((entry for entry in config.runtimes if entry.reference == selected.runtime_reference), None)
    if (
        config.instance_id != selected.new_instance
        or config.initial_runtime != selected.runtime_reference
        or config.state_root != Path(selected.state_root)
        or config.journal_root != Path(selected.operation_root)
        or config.oauth_root != Path(selected.oauth_root)
        or config.fresh_guard_path != Path(selected.guard_root) / "fresh-guard.sock"
        or config.fresh_prior_lock_path is not None
        or config.new_instance_initialization is not True
        or runtime is None
        or runtime.executable != Path(selected.executable)
        or runtime.source_root != Path(selected.application_root)
    ):
        raise ValueError("NEW selected instance, roots, runtime or application differs")
    if runtime.arguments != ("management", "serve", "--config", str(management_path)) or runtime.execution_kind != "source-script" or not runtime.verify(initial=True) or config.fresh_runtime_binding is not None or config.archived_exclusions:
        raise ValueError("NEW selected management runtime is not supported or differs")
    environment = dict(runtime.environment)
    if environment.get("PYTHONDONTWRITEBYTECODE") != "1" or any((key.startswith("PYTHON") and (key, value) not in {("PYTHONDONTWRITEBYTECODE", "1"), ("PYTHONNOUSERSITE", "1")}) or key.startswith("LD_") for key, value in environment.items()):
        raise ValueError("NEW selected runtime environment is not source-closed")
    if (
        settings.bootstrap_config_path != bootstrap_path
        or settings.state_root != config.oauth_root
        or settings.operation_state_root != config.journal_root
        or (config.management_config is not None and config.management_config != management_path)
        or READ_SCOPE not in settings.oauth.enabled_scopes
        or not any(READ_SCOPE in client.allowed_scopes for client in settings.oauth.clients)
    ):
        raise ValueError("NEW selected management or OAuth configuration differs")
    endpoint = urlsplit(config.listener_url)
    if endpoint.scheme != "http" or endpoint.hostname not in {"127.0.0.1", "::1"} or endpoint.hostname != settings.bind_host or endpoint.port != settings.port or endpoint.path != "/mcp" or endpoint.query or endpoint.fragment:
        raise ValueError("NEW selected protected management endpoint differs")
    validate_retained_oauth(settings.oauth)
    # The independent file and mounted-root receipts must remain identical
    # after the production configuration and OAuth checks have completed.
    if inspect_new_instance_origin(request_file) != origin:
        raise ValueError("NEW origin changed during profile verification")
    return NewProfileReceipt(
        request_id=selected.request_id,
        origin_sha256=origin.record_sha256,
        instance_id=config.instance_id,
        runtime_reference=runtime.reference,
        listener_url=config.listener_url,
        oauth_project=settings.oauth.firebase_project_id,
    )
