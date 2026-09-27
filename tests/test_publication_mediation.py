"""Regression coverage for invocation-owned publication mediation (Issue #2324)."""

from __future__ import annotations

from pathlib import Path

from src.auto_coder.invocation_process_supervisor import InstallationContext
from src.auto_coder.publication_mediation import (
    InferenceRoute,
    InstalledPublicationGuard,
    PublicationMediationPolicy,
    PublicationPolicyRequest,
    mediated_environment,
)


class RecordingEnforcer:
    def __init__(self, environment: dict[str, str] | None = None) -> None:
        self.environment = environment or {}
        self.request: PublicationPolicyRequest | None = None
        self.closed = False

    def install(self, request: PublicationPolicyRequest) -> InstalledPublicationGuard:
        self.request = request
        return InstalledPublicationGuard(lambda: None, self.environment, "guard active")

    def close(self) -> None:
        self.closed = True


def _context(tmp_path: Path) -> InstallationContext:
    result = tmp_path / "result"
    runtime = tmp_path / "runtime"
    ownership = tmp_path / "controller" / "invocation"
    for path in (result, runtime, ownership):
        path.mkdir(parents=True)
    return InstallationContext(
        invocation_id="invocation-2324",
        backend_type="opencode",
        effective_mode="editable",
        result_root=result,
        runtime_paths=(runtime,),
        ownership_path=ownership,
    )


def test_policy_passes_exact_invocation_provider_model_roots_and_routes(tmp_path: Path) -> None:
    enforcer = RecordingEnforcer({"PROVIDER_AUTH_FILE": "/run/controller/auth"})
    route = InferenceRoute("https", "inference.example", 443, "/v1/responses")
    policy = PublicationMediationPolicy(
        "openai",
        "configured-model",
        (route,),
        enforcer,
        {"PATH": "/usr/bin", "GH_TOKEN": "caller-secret"},
    )

    installation = policy.install(_context(tmp_path))

    assert installation.installed is True
    assert installation.establishes_publication_enforcement is True
    assert installation.establishes_violation_observation is True
    assert installation.environment == {
        "PATH": "/usr/bin",
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "Never",
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "",
        "GIT_CONFIG_KEY_1": "core.askPass",
        "GIT_CONFIG_VALUE_1": "",
        "PROVIDER_AUTH_FILE": "/run/controller/auth",
    }
    assert enforcer.request == PublicationPolicyRequest(
        invocation_id="invocation-2324",
        backend_type="opencode",
        effective_mode="editable",
        ownership_path=(tmp_path / "controller/invocation").resolve(),
        owned_roots=((tmp_path / "result").resolve(), (tmp_path / "runtime").resolve()),
        provider="openai",
        model="configured-model",
        inference_routes=(route,),
    )


def test_caller_credentials_helpers_and_forwarded_socket_are_removed() -> None:
    secret = "do-not-disclose"
    source = {
        "PATH": "/bin",
        "GH_TOKEN": secret,
        "GITHUB_TOKEN": secret,
        "GITHUB_ENTERPRISE_TOKEN": secret,
        "GIT_ASKPASS": "/tmp/askpass",
        "SSH_ASKPASS": "/tmp/ssh-askpass",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
        "GIT_CONFIG_GLOBAL": "/tmp/gitconfig",
        "GIT_CONFIG_SYSTEM": "/tmp/system-gitconfig",
    }

    environment = mediated_environment(source, InstalledPublicationGuard(lambda: None))

    assert environment["PATH"] == "/bin"
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GIT_CONFIG_VALUE_0"] == ""
    assert secret not in repr(environment)
    for forbidden in source.keys() - {"PATH"}:
        assert forbidden not in environment


def test_missing_routes_and_authority_alias_fail_before_enforcer_install(tmp_path: Path) -> None:
    context = _context(tmp_path)
    enforcer = RecordingEnforcer()
    missing = PublicationMediationPolicy("openai", "model", (), enforcer, {})

    unavailable = missing.install(context)

    assert unavailable.installed is False
    assert "inference routes are required" in unavailable.detail
    assert enforcer.request is None

    alias_context = InstallationContext(
        invocation_id=context.invocation_id,
        backend_type=context.backend_type,
        effective_mode=context.effective_mode,
        result_root=context.result_root,
        runtime_paths=context.runtime_paths,
        ownership_path=context.result_root,
    )
    aliased = PublicationMediationPolicy(
        "openai",
        "model",
        (InferenceRoute("https", "inference.example", 443, "/v1"),),
        enforcer,
        {},
    ).install(alias_context)
    assert aliased.installed is False
    assert "alias controller policy authority" in aliased.detail
    assert enforcer.request is None


def test_route_rejects_invalid_or_traversing_configuration() -> None:
    for values in (
        ("ftp", "host", 21, "/inference"),
        ("https", "", 443, "/inference"),
        ("https", "host", 0, "/inference"),
        ("https", "host", 443, "relative"),
        ("https", "host", 443, "/v1/../mutation"),
    ):
        try:
            InferenceRoute(*values)  # type: ignore[arg-type]
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe route was accepted: {values!r}")
