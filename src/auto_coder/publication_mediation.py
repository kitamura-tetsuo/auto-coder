"""Controller-owned publication and credential mediation for local invocations.

This module deliberately separates the transport enforcer from policy selection.
The enforcer is a privileged runtime component (for example an egress broker plus
network namespace) and supplies a child setup callback only after it has installed
pre-effect rules for the invocation ownership identity.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional, Protocol

from .invocation_process_supervisor import InstallationContext, PolicyInstallation


class PublicationMediationUnavailable(RuntimeError):
    """The requested invocation cannot be safely mediated before submission."""


@dataclass(frozen=True)
class InferenceRoute:
    """One controller-selected inference destination, including its path boundary."""

    scheme: str
    host: str
    port: int
    path_prefix: str

    def __post_init__(self) -> None:
        if self.scheme not in {"http", "https"} or not self.host or not (1 <= self.port <= 65535):
            raise ValueError("inference route must have a valid scheme, host, and port")
        if not self.path_prefix.startswith("/") or ".." in self.path_prefix.split("/"):
            raise ValueError("inference route requires an absolute normalized path prefix")


@dataclass(frozen=True)
class PublicationPolicyRequest:
    invocation_id: str
    backend_type: str
    effective_mode: str
    ownership_path: Path
    owned_roots: tuple[Path, ...]
    provider: str
    model: str
    inference_routes: tuple[InferenceRoute, ...]


@dataclass(frozen=True)
class InstalledPublicationGuard:
    """Capability returned only after pre-effect transport mediation is active."""

    child_setup: Callable[[], None]
    environment: Mapping[str, str] = field(default_factory=dict)
    detail: str = "publication mediation installed"


class PublicationTransportEnforcer(Protocol):
    def install(self, request: PublicationPolicyRequest) -> InstalledPublicationGuard: ...

    def close(self) -> None: ...


_CREDENTIAL_KEYS = {
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
    "SSH_AUTH_SOCK",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
}


def mediated_environment(source: Mapping[str, str], guard: InstalledPublicationGuard) -> dict[str, str]:
    """Remove caller publication authority and apply controller-owned values.

    Provider authentication is reintroduced only by the enforcer's opaque,
    controller-selected environment.  Values are never included in diagnostics.
    """

    environment = {key: value for key, value in source.items() if key not in _CREDENTIAL_KEYS}
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "",
            "GIT_CONFIG_KEY_1": "core.askPass",
            "GIT_CONFIG_VALUE_1": "",
        }
    )
    environment.update(guard.environment)
    return environment


@dataclass
class PublicationMediationPolicy:
    """Install one fail-closed publication policy on a supervised invocation."""

    provider: str
    model: str
    inference_routes: tuple[InferenceRoute, ...]
    enforcer: PublicationTransportEnforcer
    source_environment: Optional[Mapping[str, str]] = None

    def install(self, context: InstallationContext) -> PolicyInstallation:
        try:
            if context.effective_mode not in {"editable", "no-edit"}:
                raise PublicationMediationUnavailable("effective mode is not supported")
            if not self.provider.strip() or not self.model.strip() or not self.inference_routes:
                raise PublicationMediationUnavailable("provider, model, and inference routes are required")
            roots = (context.result_root, *context.runtime_paths)
            canonical_roots = tuple(path.resolve(strict=True) for path in roots)
            ownership = context.ownership_path.resolve(strict=True)
            if any(ownership == root or ownership in root.parents or root in ownership.parents for root in canonical_roots):
                raise PublicationMediationUnavailable("owned roots alias controller policy authority")
            guard = self.enforcer.install(
                PublicationPolicyRequest(
                    invocation_id=context.invocation_id,
                    backend_type=context.backend_type,
                    effective_mode=context.effective_mode,
                    ownership_path=ownership,
                    owned_roots=canonical_roots,
                    provider=self.provider,
                    model=self.model,
                    inference_routes=self.inference_routes,
                )
            )
            environment = mediated_environment(self.source_environment or os.environ, guard)
            return PolicyInstallation(
                True,
                guard.detail,
                establishes_publication_enforcement=True,
                establishes_violation_observation=True,
                child_setup=guard.child_setup,
                environment=environment,
            )
        except (OSError, ValueError, PublicationMediationUnavailable) as exc:
            return PolicyInstallation(False, f"publication mediation unavailable: {exc}")

    def close(self) -> None:
        self.enforcer.close()
