"""Real disposable Docker cutover through the operator upgrade entry points.

The external source/Actions service is the installed-fixture's tested local
authority. Only that fixture-specific authority adapter is replaced; host
Docker effects, owner locks, inherited exclusions, installed READY, OAuth and
actual authenticated management/worker reads remain production implementations.
The independent CI source workflow owns the public publication proof.
"""

from __future__ import annotations

import hashlib
import json
import os
import select
import signal
import sqlite3
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from threading import Event

import pytest

from auto_coder.management import bootstrap_upgrade as upgrade
from auto_coder.management.fresh_initialization import FreshInitializationIntent
from auto_coder.management.operation_store import ManagementOperationStore
from auto_coder.management.operations import ActionParameter, EffectReceipt, ManagedTarget, OperationIntent, OperationPhase
from auto_coder.operating_policy import MANAGEMENT_REPOSITORY
from auto_coder.tested_source_refs import TestedSourcePublisher as SourceProofPublisher
from auto_coder.util.gh_cache import GitHubGitDataClient
from tests.test_management_archived_cutover_container import Generation
from tests.test_management_installed_fresh_container import InstalledChain
from tests.test_management_installed_fresh_container import installed_chain as installed_chain
from tests.test_management_installed_fresh_container import wait_for

pytestmark = [pytest.mark.supervisor_integration, pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")]


def private(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)
    return path


def approved_profile(chain: InstalledChain, old: Generation, new: Generation) -> tuple[Path, Path, Path]:
    """Seal the exact, already created Docker objects without mutating old ones."""
    assert old.proxy is not None and new.intent is not None and new.prior_lock is not None
    caddy = new.path("Caddyfile")
    caddy.write_text("http://127.0.0.1:8080 {\n reverse_proxy 127.0.0.1:8081 {\n header_up Host 127.0.0.1:8081\n }\n}\n")
    caddy.chmod(0o600)
    proxy_name = "operator-upgrade-proxy-" + uuid.uuid4().hex[:16]
    chain.names.append(proxy_name)
    new.proxy = chain.docker(
        "create",
        "--name",
        proxy_name,
        "--network",
        "container:" + new.container,
        "--mount",
        f"type=bind,src={caddy},dst=/etc/caddy/Caddyfile,readonly",
        "--entrypoint",
        "caddy",
        chain.proxy_image,
        "run",
        "--config",
        "/etc/caddy/Caddyfile",
        "--adapter",
        "caddyfile",
    )
    old_watch, new_watch = old.path("watch"), new.path("watch")
    for directory in (old_watch, new_watch):
        directory.mkdir(mode=0o700)

    def watcher(generation: Generation, state: Path) -> Path:
        assert generation.proxy is not None
        return private(
            generation.path("proxy-watch.json"),
            {
                "state_root": str(state),
                "manager_id": generation.container,
                "manager_image": chain.image,
                "proxy_id": generation.proxy,
                "proxy_image": chain.proxy_image,
                "config_sha256": hashlib.sha256(generation.path("Caddyfile").read_bytes()).hexdigest(),
            },
        )

    old_watcher, new_watcher = watcher(old, old_watch), watcher(new, new_watch)
    extension_file = new.path("bootstrap") / "management.yaml.source-evidence.json"
    management = new.path("bootstrap") / "management.yaml"
    private(
        extension_file,
        {
            "schema_version": 1,
            "management_config_sha256": hashlib.sha256(management.read_bytes()).hexdigest(),
            "source_repository": "kitamura-tetsuo/auto-coder",
            "source_evidence_token_file": "/bootstrap/source_evidence_read",
        },
    )
    token = new.path("bootstrap") / "source_evidence_read"
    token.write_text("disposable-fixture-read-token-00001", encoding="utf-8")
    token.chmod(0o600)
    mounts = private(
        chain.root / (new.name + "-prior-mounts.json"),
        {
            "management": [[str(a), b, c] for a, b, c in old.mounts],
            "proxy": [[str(old.path("Caddyfile")), "/etc/caddy/Caddyfile", False]],
        },
    )
    successor = private(
        chain.root / (new.name + "-successor-mounts.json"),
        {
            "management": [[str(a), b, c] for a, b, c in new.mounts],
            "proxy": [[str(caddy), "/etc/caddy/Caddyfile", False]],
        },
    )
    profile = private(
        chain.root / (new.name + "-operator-profile.json"),
        asdict(
            upgrade.UpgradeProfile(
                evidence_file=str(new.path("guard") / "prior-evidence.json"),
                intent_file=str(new.intent),
                bootstrap_config=str(new.path("bootstrap") / "config.json"),
                management_config=str(management),
                prior_lock=str(new.prior_lock),
                guard_dir=str(new.path("guard")),
                mounts_file=str(mounts),
                successor_mounts_file=str(successor),
                successor_proxy_id=new.proxy,
                successor_proxy_image=chain.proxy_image,
                successor_proxy_config=str(caddy),
                old_proxy_config=str(old.path("Caddyfile")),
                old_watcher_config=str(old_watcher),
                new_watcher_config=str(new_watcher),
                source_sha=chain.current_sha,
            )
        ),
    )
    return profile, old_watcher, new_watcher


def installed_fixture_preflight(profile: upgrade.UpgradeProfile) -> None:
    """Run the real installed READY verifier against the local fixture source."""
    intent = FreshInitializationIntent(**json.loads(Path(profile.intent_file).read_text()))
    mounts = json.loads(Path(profile.successor_mounts_file).read_text())["management"]
    script = """import json
from pathlib import Path
from auto_coder.management.bootstrap_supervisor import BootstrapConfig
from auto_coder.management.fresh_initialization import FreshInitializationIntent
from auto_coder.management.fresh_runtime_binding import verify_installed_fresh_binding
from auto_coder.management.oauth_config import load_management_settings
from auto_coder.tested_source_refs import TestedSourcePublisher
from auto_coder.util.gh_cache import GitHubGitDataClient
bootstrap=Path('/bootstrap/config.json')
config=BootstrapConfig.read(bootstrap)
settings=load_management_settings(config.management_config)
intent=FreshInitializationIntent(**json.loads(Path('/upgrade-intent.json').read_text()))
external=json.loads((config.oauth_root/'external-services.json').read_text())
authority=TestedSourcePublisher(GitHubGitDataClient('',external['repository'],api_url=external['url']),external['repository'])
verify_installed_fresh_binding(config,bootstrap,intent,settings,source_authority=authority)
print('DISPOSABLE_READY_VERIFIED')
"""
    command = ["docker", "run", "--rm", "--network", "bridge", "--read-only", "--env", "PYTHONDONTWRITEBYTECODE=1"]
    for source, destination, _writable in mounts:
        command.extend(("--mount", f"type=bind,src={source},dst={destination},readonly"))
    command.extend(("--mount", f"type=bind,src={profile.intent_file},dst=/upgrade-intent.json,readonly"))
    command.extend(("--entrypoint", "python", intent.image_digest, "-I", "-B", "-c", script))
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=240)
    assert result.stdout.strip().endswith("DISPOSABLE_READY_VERIFIED")


@pytest.mark.timeout(900)
def test_disposable_operator_prepare_apply_status_real_docker(installed_chain: InstalledChain, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    chain = installed_chain
    first = chain.prepare("manager-a")
    chain.docker("start", first.container)

    def first_running() -> bool:
        try:
            return chain.child(first)["phase"] == "running"
        except (sqlite3.Error, KeyError, TypeError):
            return False

    wait_for(first_running, "first installed child")
    chain.authorize()
    chain.ready(first, fresh=False)
    chain.ensure_proxy(first)
    original_worker = chain.workers()
    original_a = chain.seed(first)
    original_worker_records = chain.seed_workers(first)

    # Independent installed-process history is written by the real child,
    # not by the host coordinator's _docker effect accounting wrapper.
    def installed_launches() -> tuple[tuple[int, str, str], ...]:
        history = chain.oauth / "installed-launches.jsonl"
        if not history.exists():
            return ()
        return tuple((entry["pid"], entry["runtime"], entry["revision"]) for entry in (json.loads(line) for line in history.read_text().splitlines()))

    initial_installed_launches = installed_launches()
    next_generation = chain.prepare("manager-b", first)
    profile_path, _, new_watcher = approved_profile(chain, first, next_generation)
    auth_file = tmp_path / "operator-auth-client"
    auth_file.write_text(chain.token)
    auth_file.chmod(0o600)
    operator = tmp_path / "host-upgrades"
    operator.mkdir(mode=0o700)
    monkeypatch.setattr(upgrade, "OPERATOR_ROOT", operator)
    publisher = SourceProofPublisher(GitHubGitDataClient("", chain.external["repository"], api_url=chain.external["url"]), chain.external["repository"])
    monkeypatch.setattr(upgrade, "_source_authority", lambda _token: publisher)

    def fixture_source_proof(authority: SourceProofPublisher, source_sha: str) -> tuple[str, int, int]:
        proof = authority.read(source_sha)
        assert proof is not None
        authority.verify_run(proof.record, recorded_attempt=True)
        return proof.tag_object_oid, proof.record.run_id, proof.record.run_attempt

    monkeypatch.setattr(upgrade, "_source_proof", fixture_source_proof)
    monkeypatch.setattr(upgrade, "_candidate_image_source", lambda _container, _image: chain.current_sha)
    monkeypatch.setattr(upgrade, "_installed_candidate_preflight", installed_fixture_preflight)
    actual_docker = upgrade._docker
    effects = []
    first_stop_entered = Event()
    first_stop_released = Event()

    def counted_effect(*args: str, timeout: float = 30) -> str:
        if args == ("kill", "--signal=SIGTERM", first.proxy):
            first_stop_entered.set()
            assert first_stop_released.wait(90), "real predecessor-stop barrier was not released"
        if args and args[0] in {"kill", "start"}:
            effects.append(args)
        return actual_docker(*args, timeout=timeout)

    monkeypatch.setattr(upgrade, "_docker", counted_effect)

    def call_cli(*args: str) -> tuple[int, dict[str, object]]:
        monkeypatch.setattr(sys, "argv", ["bootstrap_upgrade", *args])
        result = upgrade.main()
        lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
        assert lines, "real operator command did not emit its protected result"
        return result, json.loads(lines[-1])

    try:
        code, prepared = call_cli("prepare", "--profile", str(profile_path), "--auth-token-file", str(auth_file), "--authorize-source-read")
        assert code == 0 and prepared["stage"] == "prepared"
        assert effects == [] and chain.workers() == original_worker
        plan_id = prepared["plan_sha256"]
        assert isinstance(plan_id, str)
        old_snapshot = chain.snapshot(first)
        # Refuse changed protected configuration before any container effect.
        management_config = next_generation.path("bootstrap") / "management.yaml"
        original_config = management_config.read_bytes()
        try:
            management_config.write_bytes(original_config + b"\n# disposable changed-config boundary\n")
            code, denied = call_cli("apply", "--plan-sha256", plan_id, "--auth-token-file", str(auth_file), "--authorize-source-read")
            assert code == 2 and denied["stage"] == "prerequisite-blocked"
            assert effects == [] and chain.snapshot(first) == old_snapshot
        finally:
            management_config.write_bytes(original_config)

        # A different candidate image binding is not an approved effect input.
        # Alter the immutable fresh intent only on this disposable candidate.
        candidate_intent = next_generation.intent
        assert candidate_intent is not None
        original_intent = candidate_intent.read_bytes()
        try:
            changed_intent = json.loads(original_intent)
            changed_intent["image_digest"] = "sha256:" + "0" * 64
            candidate_intent.write_text(json.dumps(changed_intent))
            code, denied = call_cli("apply", "--plan-sha256", plan_id, "--auth-token-file", str(auth_file), "--authorize-source-read")
            assert code == 2 and denied["stage"] == "prerequisite-blocked"
            assert effects == [] and chain.snapshot(first) == old_snapshot
        finally:
            candidate_intent.write_bytes(original_intent)

        # A missing already-authorized protected reader must not be silently
        # substituted with a host token or lead to a predecessor stop.
        source_credential = next_generation.path("bootstrap") / "source_evidence_read"
        absent_credential = next_generation.path("bootstrap") / "source_evidence_read.absent"
        source_credential.rename(absent_credential)
        try:
            code, denied = call_cli("apply", "--plan-sha256", plan_id, "--auth-token-file", str(auth_file), "--authorize-source-read")
            assert code == 2 and denied["stage"] == "prerequisite-blocked"
            assert effects == [] and chain.snapshot(first) == old_snapshot
        finally:
            absent_credential.rename(source_credential)

        # Unavailable exact-source/Actions evidence and absent explicit consent
        # must each block the incumbent-preserving operation.
        chain.services.unavailable = True
        try:
            code, denied = call_cli("apply", "--plan-sha256", plan_id, "--auth-token-file", str(auth_file), "--authorize-source-read")
            assert code == 2 and denied["stage"] == "prerequisite-blocked"
            assert effects == [] and chain.snapshot(first) == old_snapshot
        finally:
            chain.services.unavailable = False
        code, denied = call_cli("apply", "--plan-sha256", plan_id, "--auth-token-file", str(auth_file))
        assert code == 2 and denied["stage"] == "prerequisite-blocked"
        assert effects == [] and chain.workers() == original_worker

        # Two real operator apply invocations must contend on the SAME held
        # global lane while the first is suspended at a physical-effect
        # barrier, rather than merely appearing concurrent after completion.
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(call_cli, "apply", "--plan-sha256", plan_id, "--auth-token-file", str(auth_file), "--authorize-source-read")
            try:
                assert first_stop_entered.wait(120), "real stop dispatch never reached the live barrier"
                assert effects == [] and chain.workers() == original_worker
                with pytest.raises(upgrade.UpgradeUnavailable, match="live operator"):
                    upgrade.apply(operator, plan_id, auth_file, authorize_source_read=True)
                assert effects == [] and chain.workers() == original_worker
            finally:
                first_stop_released.set()
            code, applied = pending.result(timeout=240)
        assert code == 0 and applied["stage"] == "successor-launch-uncertain"
        assert effects == [
            ("kill", "--signal=SIGTERM", first.proxy),
            ("kill", "--signal=SIGTERM", first.container),
            ("start", next_generation.container),
            ("start", next_generation.proxy),
        ]
        assert chain.snapshot(first) == old_snapshot
        assert chain.docker("inspect", "--format", "{{.State.Running}}", first.container) == "false"
        assert chain.docker("inspect", "--format", "{{.State.Running}}", first.proxy) == "false"
        assert chain.workers() == original_worker

        def application_ready() -> bool:
            _, result = call_cli("status", "--plan-sha256", plan_id, "--auth-token-file", str(auth_file))
            return result["stage"] == "application-ready"

        wait_for(application_ready, "protected exact-child application readiness", timeout=90)
        b_installed_launches = installed_launches()
        assert b_installed_launches[: len(initial_installed_launches)] == initial_installed_launches
        assert len(b_installed_launches) == len(initial_installed_launches) + 1, "B must launch exactly one installed child"
        # Read-only beta eligibility must come from the *running upgraded
        # service's* configured BetaEvidenceReader, over the authenticated MCP
        # endpoint. The fixture has real Git objects and locally executed tests,
        # not substituted public GitHub Actions/producer proof.
        before_readback_posts = chain.worker_posts()
        code, beta_status = call_cli("status", "--plan-sha256", plan_id, "--auth-token-file", str(auth_file), "--authorize-source-read")
        assert code == 0 and beta_status["stage"] == "beta-deploy-prerequisites-verified"
        assert beta_status["selected_source_sha"] == chain.current_sha
        assert beta_status["selection_operation_id"] == "r101"
        assert upgrade._operation(operator, plan_id).stage == "beta-deploy-prerequisites-verified"
        assert len(effects) == 4 and chain.workers() == original_worker
        assert chain.worker_posts() == before_readback_posts
        # Prove the host can still read real local source proof while the
        # upgraded child's independently configured credential is invalid.
        # The host's successful proof must never substitute for child readback.
        service_credential = next_generation.path("bootstrap") / "source_evidence_read"
        service_token = service_credential.read_bytes()
        try:
            service_credential.write_text("invalid-disposable-source-token", encoding="utf-8")
            assert publisher.read(chain.current_sha) is not None
            code, service_unavailable = call_cli("status", "--plan-sha256", plan_id, "--auth-token-file", str(auth_file), "--authorize-source-read")
            assert code == 0 and service_unavailable["stage"] == "application-ready"
            assert "upgraded-service beta source readback" in service_unavailable["missing_prerequisites"][0]
            assert len(effects) == 4 and chain.worker_posts() == before_readback_posts
            assert installed_launches() == b_installed_launches
        finally:
            service_credential.write_bytes(service_token)

        # Keep the host's tested-source fixture intact while the actual child
        # reader loses the beta selection. A host-only proof must not certify
        # the upgraded service's eligibility.
        selection_ref = "refs/tags/auto-coder-beta-selection-current"
        selected_oid = subprocess.run(
            ["git", "rev-parse", selection_ref],
            cwd=chain.services.repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(["git", "update-ref", "-d", selection_ref], cwd=chain.services.repository, check=True)
        try:
            code, unavailable = call_cli("status", "--plan-sha256", plan_id, "--auth-token-file", str(auth_file), "--authorize-source-read")
            assert code == 0 and unavailable["stage"] == "application-ready"
            assert upgrade._operation(operator, plan_id).stage == "application-ready"
            assert unavailable["missing_prerequisites"]
            assert len(effects) == 4 and chain.worker_posts() == before_readback_posts
        finally:
            subprocess.run(["git", "update-ref", selection_ref, selected_oid], cwd=chain.services.repository, check=True)
        # A mismatched direct beta pointer must also be rejected by the
        # service's real selection reader without admitting worker effects.
        beta_ref = "refs/tags/auto-coder-beta-current"
        subprocess.run(["git", "update-ref", beta_ref, chain.candidate_sha], cwd=chain.services.repository, check=True)
        try:
            code, conflicting = call_cli("status", "--plan-sha256", plan_id, "--auth-token-file", str(auth_file), "--authorize-source-read")
            assert code == 0 and conflicting["stage"] == "application-ready"
            assert upgrade._operation(operator, plan_id).stage == "application-ready"
            assert conflicting["missing_prerequisites"]
            assert len(effects) == 4 and chain.worker_posts() == before_readback_posts
        finally:
            subprocess.run(["git", "update-ref", beta_ref, chain.current_sha], cwd=chain.services.repository, check=True)
        code, restored = call_cli("status", "--plan-sha256", plan_id, "--auth-token-file", str(auth_file), "--authorize-source-read")
        assert code == 0 and restored["stage"] == "beta-deploy-prerequisites-verified"
        assert upgrade._operation(operator, plan_id).stage == "beta-deploy-prerequisites-verified"
        assert restored["selected_source_sha"] == chain.current_sha
        assert len(effects) == 4 and chain.worker_posts() == before_readback_posts

        # A completed Docker cutover alone cannot authorize a dead/revoked
        # authenticated client. This failure must not reuse the previous
        # successful source readback or trigger another installed launch.
        original_client = auth_file.read_bytes()
        try:
            auth_file.write_text("invalid-disposable-authentication", encoding="utf-8")
            code, no_auth = call_cli("status", "--plan-sha256", plan_id, "--auth-token-file", str(auth_file), "--authorize-source-read")
            assert code == 2 and no_auth["stage"] == "needs-attention"
            assert installed_launches() == b_installed_launches
            assert len(effects) == 4 and chain.worker_posts() == before_readback_posts
        finally:
            auth_file.write_bytes(original_client)
        code, authenticated_again = call_cli("status", "--plan-sha256", plan_id, "--auth-token-file", str(auth_file), "--authorize-source-read")
        assert code == 0 and authenticated_again["stage"] == "beta-deploy-prerequisites-verified"
        assert installed_launches() == b_installed_launches
        code, repeated = call_cli("apply", "--plan-sha256", plan_id, "--auth-token-file", str(auth_file), "--authorize-source-read")
        assert code == 2 and repeated["stage"] == "needs-attention"
        assert len(effects) == 4 and chain.workers() == original_worker
        assert chain.call(next_generation, "auto_coder_whoami", {})["uid"] == "operator"
        assert chain.call(next_generation, "auto_coder_operation_get", {"operation_id": original_a.operation_id})["history_origin"] == "retained_prior_instance"

        # B is now the actual online predecessor. Explicitly stop only its
        # known disposable watcher under the operator's service-owner boundary.
        old_b = chain.seed(next_generation)
        # B must own distinct direct-prior worker keys; A's existing keys are
        # already archived and cannot be substituted as B's replay records.
        b_store = ManagementOperationStore(next_generation.path("journal"))
        b_worker_records = {}
        for channel, original in original_worker_records.items():
            intent = original.intent
            b_intent = OperationIntent(
                intent.target,
                intent.action,
                intent.parameters,
                "b-prior-worker-" + channel + "-" + uuid.uuid4().hex,
                intent.expected_incarnation,
                intent.expected_artifact,
            )
            admitted = b_store.admit(b_intent, chain.authority).record
            assert admitted is not None
            assert (
                b_store.transition(
                    admitted.operation_id,
                    0,
                    OperationPhase.FAILED,
                    evidence=EffectReceipt(admitted.operation_id, "synthetic-no-effect", 1, "no_effect_sent"),
                ).record
                is not None
            )
            b_worker_records[channel] = admitted
        chain.checkpoint(next_generation.path("journal") / "management-operations.sqlite3")
        snapshot_b = chain.snapshot(next_generation)
        b_operation = upgrade._operation(operator, plan_id)
        assert b_operation.stage == "application-ready" and b_operation.watcher_pid > 0
        assert upgrade._watcher_lifetime(b_operation.watcher_pid) == b_operation.watcher_start_token
        assert upgrade._watcher_is_owned(new_watcher.parent / "watch", b_operation, new_watcher)
        os.kill(b_operation.watcher_pid, signal.SIGTERM)

        def b_watcher_exited() -> bool:
            try:
                upgrade._watcher_lifetime(b_operation.watcher_pid)
            except (OSError, ValueError):
                return True
            return False

        wait_for(b_watcher_exited, "disposable prior watcher exit", timeout=20)
        os.waitpid(b_operation.watcher_pid, 0)
        assert chain.workers() == original_worker

        third = chain.prepare("manager-c", next_generation)
        profile_c, _, _ = approved_profile(chain, next_generation, third)
        code, c_prepared = call_cli("prepare", "--profile", str(profile_c), "--auth-token-file", str(auth_file), "--authorize-source-read")
        assert code == 0 and c_prepared["stage"] == "prepared"
        c_plan = c_prepared["plan_sha256"]
        assert isinstance(c_plan, str) and c_plan != plan_id
        assert len(effects) == 4 and chain.workers() == original_worker
        code, c_applied = call_cli("apply", "--plan-sha256", c_plan, "--auth-token-file", str(auth_file), "--authorize-source-read")
        assert code == 0 and c_applied["stage"] == "successor-launch-uncertain"
        assert effects[4:] == [
            ("kill", "--signal=SIGTERM", next_generation.proxy),
            ("kill", "--signal=SIGTERM", next_generation.container),
            ("start", third.container),
            ("start", third.proxy),
        ]
        assert chain.snapshot(first) == old_snapshot
        assert chain.snapshot(next_generation) == snapshot_b
        assert chain.workers() == original_worker

        def c_application_ready() -> bool:
            _, result = call_cli("status", "--plan-sha256", c_plan, "--auth-token-file", str(auth_file))
            return result["stage"] == "application-ready"

        wait_for(c_application_ready, "C installed child, archived A and direct-prior B", timeout=90)
        c_installed_launches = installed_launches()
        assert c_installed_launches[: len(b_installed_launches)] == b_installed_launches
        assert len(c_installed_launches) == len(b_installed_launches) + 1, "C must launch exactly one installed child"
        # Beta prerequisite verification also survives the second physical
        # upgrade. Neither the host command nor this readback deploys beta.
        worker_posts_c = chain.worker_posts()
        code, c_beta = call_cli("status", "--plan-sha256", c_plan, "--auth-token-file", str(auth_file), "--authorize-source-read")
        assert code == 0 and c_beta["stage"] == "beta-deploy-prerequisites-verified"
        assert c_beta["selected_source_sha"] == chain.current_sha
        assert c_beta["selection_operation_id"] == "r101"
        assert chain.worker_posts() == worker_posts_c
        chain.assert_workers(third, original_worker_records)
        assert chain.grants() == chain.grants_before
        assert chain.worker_posts() == {"beta": 1, "release": 1}
        assert chain.call(third, "auto_coder_whoami", {})["uid"] == "operator"
        for previous in (first, next_generation):
            chain.owner_refused(previous)
        # B is the direct previous history; A is an immutable archived
        # exclusion, not a source of re-admissible old idempotency keys.
        response_b = chain.call(third, "auto_coder_update_management_service", {"source_sha": "a" * 40, "idempotency_key": old_b.intent.idempotency_key})
        assert response_b["operation_id"] == old_b.operation_id and response_b["history_origin"] == "retained_prior_instance"
        assert chain.call(third, "auto_coder_operation_get", {"operation_id": old_b.operation_id})["history_origin"] == "retained_prior_instance"
        # The direct-prior worker namespace replays B's original result. It
        # must not dispatch a fresh worker request or read A's archived result.
        prior_posts = chain.worker_posts()
        for channel, record in b_worker_records.items():
            replayed = chain.call(
                third,
                "auto_coder_reevaluate_target",
                {
                    "target_id": channel + "-worker",
                    "target_type": "issue",
                    "number": 2603,
                    "idempotency_key": record.intent.idempotency_key,
                    "expected_process_run_id": record.intent.expected_incarnation,
                    "expected_deployment_generation": record.intent.expected_artifact,
                },
            )
            assert replayed["operation_id"] == record.operation_id
            assert replayed["history_origin"] == "retained_prior_instance"
        assert chain.worker_posts() == prior_posts
        response_a = chain.call(third, "auto_coder_update_management_service", {"source_sha": "a" * 40, "idempotency_key": original_a.intent.idempotency_key})
        assert response_a["status"] == "CONFLICT" and response_a.get("operation_id") is None
        assert chain.call(third, "auto_coder_operation_get", {"operation_id": original_a.operation_id})["status"] == "NOT_FOUND"
        assert chain.snapshot(first) == old_snapshot and chain.snapshot(next_generation) == snapshot_b
        assert chain.workers() == original_worker
        # Only after checking preserved lifetimes and archived/direct-prior
        # exclusions, prove a genuinely unrelated new worker key is usable.
        fresh_record = original_worker_records["beta"]
        fresh = chain.call(
            third,
            "auto_coder_reevaluate_target",
            {
                "target_id": "beta-worker",
                "target_type": "issue",
                "number": 2603,
                "idempotency_key": "c-unrelated-worker-" + uuid.uuid4().hex,
                "expected_process_run_id": fresh_record.intent.expected_incarnation,
                "expected_deployment_generation": fresh_record.intent.expected_artifact,
            },
        )
        assert isinstance(fresh.get("operation_id"), str) and fresh["operation_id"]
        code, replay = call_cli("apply", "--plan-sha256", c_plan, "--auth-token-file", str(auth_file), "--authorize-source-read")
        assert code == 2 and replay["stage"] == "needs-attention" and len(effects) == 8
        assert installed_launches() == c_installed_launches, "replayed apply must not launch another installed child"
    finally:
        for record in operator.glob("*/upgrade-operation.json"):
            try:
                operation = json.loads(record.read_text())
                pid = operation.get("watcher_pid")
                if type(pid) is int and pid > 0:
                    cmdline = (Path("/proc") / str(pid) / "cmdline").read_bytes()
                    if os.fsencode(new_watcher) in cmdline:
                        os.kill(pid, signal.SIGTERM)
            except (OSError, ValueError):
                pass


@pytest.mark.timeout(900)
@pytest.mark.parametrize("failure_point", ("lost-stop", "lost-start", "postlaunch-proxy-failed", "retained-unresolved-work", "predecessor-generation", "host-process-interrupted"))
def test_disposable_operator_failure_fences_real_docker(installed_chain: InstalledChain, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure_point: str) -> None:
    """Dispatch real Docker effects, then lose an acknowledgement or the new proxy."""
    chain = installed_chain
    first = chain.prepare("manager-a")
    chain.docker("start", first.container)

    def first_running() -> bool:
        try:
            return chain.child(first)["phase"] == "running"
        except (sqlite3.Error, KeyError, TypeError):
            return False

    wait_for(first_running, "failure fixture installed child")
    chain.authorize()
    chain.ready(first, fresh=False)
    chain.ensure_proxy(first)
    baseline = chain.workers()
    chain.seed(first)
    successor = chain.prepare("manager-b", first)
    profile, _, _ = approved_profile(chain, first, successor)
    auth_file = tmp_path / "operator-auth-client"
    auth_file.write_text(chain.token)
    auth_file.chmod(0o600)
    operator = tmp_path / "host-upgrades"
    operator.mkdir(mode=0o700)
    monkeypatch.setattr(upgrade, "OPERATOR_ROOT", operator)
    publisher = SourceProofPublisher(GitHubGitDataClient("", chain.external["repository"], api_url=chain.external["url"]), chain.external["repository"])
    monkeypatch.setattr(upgrade, "_source_authority", lambda _token: publisher)

    def fixture_source_proof(authority: SourceProofPublisher, source_sha: str) -> tuple[str, int, int]:
        proof = authority.read(source_sha)
        assert proof is not None
        authority.verify_run(proof.record, recorded_attempt=True)
        return proof.tag_object_oid, proof.record.run_id, proof.record.run_attempt

    monkeypatch.setattr(upgrade, "_source_proof", fixture_source_proof)
    monkeypatch.setattr(upgrade, "_candidate_image_source", lambda _container, _image: chain.current_sha)
    monkeypatch.setattr(upgrade, "_installed_candidate_preflight", installed_fixture_preflight)
    actual_docker = upgrade._docker
    effects: list[tuple[str, ...]] = []

    def fault_after_real_docker(*args: str, timeout: float = 30) -> str:
        if args and args[0] in {"kill", "start"}:
            effects.append(args)
        answer = actual_docker(*args, timeout=timeout)
        if failure_point == "lost-stop" and args == ("kill", "--signal=SIGTERM", first.container):
            raise subprocess.TimeoutExpired("docker", timeout)
        if failure_point == "lost-start" and args == ("start", successor.container):
            raise subprocess.TimeoutExpired("docker", timeout)
        if failure_point == "postlaunch-proxy-failed" and args == ("start", successor.proxy):
            # The physical replacement proxy dies before the watcher proves it.
            actual_docker("kill", "--signal=SIGTERM", successor.proxy)
        return answer

    monkeypatch.setattr(upgrade, "_docker", fault_after_real_docker)

    def call_cli(*args: str) -> tuple[int, dict[str, object]]:
        monkeypatch.setattr(sys, "argv", ["bootstrap_upgrade", *args])
        code = upgrade.main()
        output = [line for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
        assert output, "operator command must retain a typed failure"
        return code, json.loads(output[-1])

    try:
        code, prepared = call_cli("prepare", "--profile", str(profile), "--auth-token-file", str(auth_file), "--authorize-source-read")
        assert code == 0 and prepared["stage"] == "prepared"
        plan_id = prepared["plan_sha256"]
        assert isinstance(plan_id, str)
        if failure_point == "retained-unresolved-work":
            # Admit real pending work after the immutable plan is prepared.
            journal = ManagementOperationStore(first.path("journal"))
            pending = journal.admit(
                OperationIntent(
                    ManagedTarget(first.name, "management", MANAGEMENT_REPOSITORY),
                    "management-service-self-update",
                    (ActionParameter("source_sha", "a" * 40),),
                    "unresolved-fixture-" + uuid.uuid4().hex,
                ),
                chain.authority,
            ).record
            assert pending is not None and pending.phase is OperationPhase.ACCEPTED
            prior_snapshot = chain.snapshot(first)
            code, denied = call_cli("apply", "--plan-sha256", plan_id, "--auth-token-file", str(auth_file), "--authorize-source-read")
            assert code == 2 and denied["stage"] == "prerequisite-blocked"
            assert "unresolved operations" in denied["reason"]
            assert effects == [] and chain.snapshot(first) == prior_snapshot
            assert journal.get(pending.operation_id).record.phase is OperationPhase.ACCEPTED
            for container in (first.container, first.proxy):
                assert chain.docker("inspect", "--format", "{{.State.Running}}", container) == "true"
            assert chain.workers() == baseline
            return
        prior_snapshot = chain.snapshot(first)
        if failure_point == "predecessor-generation":
            # A new process generation under the same immutable Docker
            # container ID is NOT the predecessor the operator approved.
            original_start = chain.docker("inspect", "--format", "{{.State.StartedAt}}", first.proxy)
            chain.docker("restart", first.proxy)
            changed_start = chain.docker("inspect", "--format", "{{.State.StartedAt}}", first.proxy)
            assert changed_start != original_start
            code, refused = call_cli("apply", "--plan-sha256", plan_id, "--auth-token-file", str(auth_file), "--authorize-source-read")
            assert code == 2 and refused["stage"] == "prerequisite-blocked"
            assert effects == [] and chain.workers() == baseline
            assert chain.snapshot(first) == prior_snapshot
            assert chain.docker("inspect", "--format", "{{.State.Running}}", first.container) == "true"
            assert chain.docker("inspect", "--format", "{{.State.Running}}", first.proxy) == "true"
            return
        if failure_point == "host-process-interrupted":
            # A disposable forked controller really holds the ancestor fence
            # and dispatches SIGTERM to the exact old proxy. Killing that host
            # controller drops the continuous locks without authorizing replay.
            read_fd, write_fd = os.pipe()
            child = os.fork()
            if child == 0:
                os.close(read_fd)

                def pause_after_real_stop(*args: str, timeout: float = 30) -> str:
                    answer = actual_docker(*args, timeout=timeout)
                    if args == ("kill", "--signal=SIGTERM", first.proxy):
                        os.write(write_fd, b"1")
                        signal.pause()
                    return answer

                upgrade._docker = pause_after_real_stop
                try:
                    upgrade.apply(operator, plan_id, auth_file, authorize_source_read=True)
                except BaseException:
                    os._exit(41)
                os._exit(42)
            os.close(write_fd)
            try:
                poll = select.poll()
                poll.register(read_fd, select.POLLIN | select.POLLHUP)
                assert poll.poll(120000), "disposable controller never reached the actual stop"
                assert os.read(read_fd, 1) == b"1", "controller stopped before the expected physical effect"
            finally:
                os.kill(child, signal.SIGKILL)
                os.waitpid(child, 0)
                os.close(read_fd)
            persisted = upgrade._operation(operator, plan_id)
            assert persisted.stage == "stopping"
            assert persisted.effect == "stop-proxy" and persisted.effect_target == first.proxy
            assert persisted.completed == ()
            assert chain.docker("inspect", "--format", "{{.State.Running}}", first.proxy) == "false"
            assert chain.docker("inspect", "--format", "{{.State.Running}}", first.container) == "true"
            assert chain.snapshot(first) == prior_snapshot and chain.workers() == baseline
            code, observed = call_cli("status", "--plan-sha256", plan_id, "--auth-token-file", str(auth_file))
            assert code == 2 and observed["stage"] == "needs-attention"
            assert observed["uncertain_effect"] == "stop-proxy"
            assert observed["uncertain_target"] == first.proxy
            assert observed["physical_effect_observation"] == "same-predecessor-stopped"
            code, replay = call_cli("apply", "--plan-sha256", plan_id, "--auth-token-file", str(auth_file), "--authorize-source-read")
            assert code == 2 and replay["stage"] == "needs-attention"
            assert upgrade._operation(operator, plan_id) == persisted
            assert chain.docker("inspect", "--format", "{{.State.Running}}", successor.container) == "false"
            assert chain.workers() == baseline
            return
        code, failed = call_cli("apply", "--plan-sha256", plan_id, "--auth-token-file", str(auth_file), "--authorize-source-read")
        assert code == 2 and failed["stage"] in {"needs-attention", "prerequisite-blocked"}
        persisted = upgrade._operation(operator, plan_id)
        assert persisted.stage == "needs-attention"
        if failure_point == "lost-stop":
            assert persisted.completed == ("stop-proxy",)
            assert persisted.effect == "stop-management" and persisted.effect_target == first.container
            assert len(effects) == 2
        elif failure_point == "lost-start":
            assert persisted.completed == ("stop-proxy", "stop-management")
            assert persisted.effect == "start-management" and persisted.effect_target == successor.container
            assert len(effects) == 3
        else:
            assert persisted.completed == ("stop-proxy", "stop-management", "start-management", "start-proxy")
            assert persisted.effect == "" and len(effects) == 4
            assert chain.docker("inspect", "--format", "{{.State.Running}}", successor.proxy) == "false"
        assert chain.snapshot(first) == prior_snapshot and chain.workers() == baseline
        code, reported = call_cli("status", "--plan-sha256", plan_id, "--auth-token-file", str(auth_file))
        assert code == 2 and reported["stage"] == "needs-attention"
        assert reported["uncertain_effect"] == persisted.effect
        if persisted.effect:
            assert reported["uncertain_target"] == persisted.effect_target
        before_retry = tuple(effects)
        # Docker's own persisted start times, unlike the wrapper counter,
        # detect a restarted successor after an ambiguous physical response.
        successor_start_times = {container: chain.docker("inspect", "--format", "{{.State.StartedAt}}", container) for container in (successor.container, successor.proxy)}
        code, replay = call_cli("apply", "--plan-sha256", plan_id, "--auth-token-file", str(auth_file), "--authorize-source-read")
        assert code == 2 and replay["stage"] == "needs-attention"
        assert tuple(effects) == before_retry
        assert {container: chain.docker("inspect", "--format", "{{.State.StartedAt}}", container) for container in (successor.container, successor.proxy)} == successor_start_times
        assert upgrade._operation(operator, plan_id) == persisted
        assert chain.workers() == baseline
    finally:
        # The installed_chain fixture exclusively owns disposable cleanup.
        # Calling close() here also would close the same namespace FDs twice.
        chain.gate_release.set()
