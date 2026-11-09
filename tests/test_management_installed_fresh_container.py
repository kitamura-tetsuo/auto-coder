"""Installed-first A/B/C through real MCP, dispatcher and durable handoff.

Only the existing isolated root CI job executes Docker. External Firebase/GitHub
endpoints are disposable HTTP services, backed by actually tested fixture Git
commits. No production credentials, stores, effects or READY verifier are mocked.
The bootstrap image adapter only supplies the trusted source-authority constructor
argument; every fence, claim, launch and retained reader is production code.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.error
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from auto_coder.beta_source_selection import BetaSelectionRecord
from auto_coder.management.self_update_handoff import HandoffPhase, HandoffStore
from auto_coder.tested_source_refs import tag_name
from tests.test_management_archived_cutover_container import Chain
from tests.test_management_fresh_end_to_end import _build_fixture_image
from tests.test_management_self_update_integration import ExternalServices, _snapshot_source, _test_source

pytestmark = [pytest.mark.supervisor_integration, pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")]

_BOOTSTRAP_AUTHORITY = """    from auto_coder.tested_source_refs import TestedSourcePublisher
    from auto_coder.util.gh_cache import GitHubGitDataClient
    external = json.loads((config.oauth_root / 'external-services.json').read_text())
    authority = TestedSourcePublisher(GitHubGitDataClient('', external['repository'], api_url=external['url']), external['repository'])
"""
_STAGE = """import json, sys
from dataclasses import asdict
from pathlib import Path
from auto_coder.management.bootstrap_supervisor import BootstrapConfig
from auto_coder.management.configured_stager import configured_management_stager
from auto_coder.management.oauth_config import load_management_settings
from auto_coder.management.self_update_handoff import runtime_for_receipt
from auto_coder.management.version_staging import ManagementPrepareIntent
from auto_coder.tested_source_refs import TestedSourcePublisher
from auto_coder.util.gh_cache import GitHubGitDataClient
settings=load_management_settings(Path('/bootstrap/management.yaml'))
config=BootstrapConfig.read(Path('/bootstrap/config.json'))
external=json.loads((config.oauth_root/'external-services.json').read_text())
authority=TestedSourcePublisher(GitHubGitDataClient('',external['repository'],api_url=external['url']), external['repository'])
stager=configured_management_stager(settings,config,source_authority=authority)
receipts=[stager.prepare(ManagementPrepareIntent(sha,'preclaim-'+str(i))).receipt for i,sha in enumerate(sys.argv[1:])]
print(json.dumps({'receipts':[asdict(r) for r in receipts], 'runtimes':[{**asdict(runtime_for_receipt(config,r)), 'environment':dict(runtime_for_receipt(config,r).environment)} for r in receipts]},default=str))
"""


def _bind_disposable_source_reader(package: Path) -> None:
    """Bind only the fixture's source transport, not its evidence producer."""
    lifecycle = package / "management" / "worker_lifecycle.py"
    original = lifecycle.read_text()
    constructor = "return BetaEvidenceReader(GitHubGitDataClient(secret, config.source_repository), config.source_repository)"
    assert original.count(constructor) == 1
    # The staged fixture source is committed and locally tested *after* this
    # adapter; it never substitutes for public GitHub Actions provenance.
    lifecycle.write_text(
        original.replace(
            constructor,
            "external = json.loads((config.oauth.database_path.parent / 'external-services.json').read_text())\n" "        return BetaEvidenceReader(GitHubGitDataClient(secret, config.source_repository, api_url=external['url']), config.source_repository)",
        )
    )


# Only fixed exception *types* may leave the disposable worker log. Neither
# traceback frames nor exception messages (which might contain credentials)
# are published into GitHub Actions output.
_ALLOWED_WORKER_EXCEPTION_TYPES = frozenset(
    {
        "assertionerror",
        "attributeerror",
        "calledprocesserror",
        "connectionerror",
        "filenotfounderror",
        "httperror",
        "httpstatuserror",
        "importerror",
        "integrityerror",
        "jsondecodeerror",
        "keyerror",
        "modulenotfounderror",
        "nameerror",
        "operationalerror",
        "oserror",
        "permissionerror",
        "runtimeerror",
        "sslerror",
        "systemexit",
        "timeout",
        "timeouterror",
        "typeerror",
        "validationerror",
        "valueerror",
    }
)


def _safe_worker_exception_types(log_text: str) -> list[str]:
    """Return allowlisted exception class names only; never return log text."""
    found: set[str] = set()
    for line in log_text.lower().splitlines()[-500:]:
        label = line.strip().partition(":")[0].rsplit(".", 1)[-1]
        if label in _ALLOWED_WORKER_EXCEPTION_TYPES:
            found.add(label)
    return sorted(found)


def test_disposable_worker_error_diagnostics_never_include_sensitive_messages() -> None:
    log_text = "\n".join(
        (
            "Traceback (most recent call last):",
            "  File /sensitive/path, line 1, in main",
            "requests.exceptions.ConnectionError: token=do-not-publish-this-value",
            "some.unknown.Exception: secret=also-private",
        )
    )
    assert _safe_worker_exception_types(log_text) == ["connectionerror"]
    assert _safe_worker_exception_types("unclassified secret=do-not-publish") == []


class InstalledChain(Chain):
    def worker_compose_profile(self, profile: dict) -> dict:
        """Use the real producer only for opt-in installed worker coverage."""
        if not self.installed_ready_beta:
            return profile
        # The installed process-issues child uses the production GitHub HTTP
        # transport during startup reconciliation. Keep the HTTPS endpoint
        # entirely inside this disposable worker network; unlike the bootstrap
        # bridge fixture it must actually complete a real producer loop.
        trust = self.root / "workers" / "ci-trust"
        trust.mkdir(mode=0o700)
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(trust / "github.key"),
                "-out",
                str(trust / "github.crt"),
                "-subj",
                "/CN=api.github.com",
                "-addext",
                "subjectAltName=DNS:api.github.com",
                "-days",
                "1",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        (trust / "github.key").chmod(0o600)
        profile["services"]["github-ci"] = {
            "image": profile["services"]["beta"]["image"],
            "entrypoint": ["python", "/fixtures/supervisor_fake_github_https.py"],
            "user": f"{os.getuid()}:{os.getgid()}",
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "sysctls": ["net.ipv4.ip_unprivileged_port_start=0"],
            "volumes": [
                f"{trust}:/ci-trust:ro",
                f"{Path(__file__).parent / 'fixtures'}:/fixtures:ro",
            ],
            "networks": {"private": {"aliases": ["api.github.com"]}},
            "healthcheck": {
                "test": [
                    "CMD",
                    "python",
                    "-c",
                    "import socket,ssl; ssl.create_default_context(cafile='/ci-trust/github.crt').wrap_socket(socket.create_connection(('127.0.0.1',443),timeout=3),server_hostname='api.github.com').close()",
                ],
                "interval": "1s",
                "timeout": "5s",
                "retries": 30,
            },
        }
        beta = profile["services"]["beta"]
        beta["depends_on"] = {"github-ci": {"condition": "service_healthy"}}
        beta["entrypoint"] = ["python", "/fixtures/supervisor_offline_pid1.py"]
        beta["environment"].update(
            {
                "AUTO_CODER_FIXTURE_HOLD_BOOTSTRAP": "1",
                "AUTO_CODER_SOURCE_REPOSITORY_ROOT": "/source",
                "AUTO_CODER_CONFIG_PATH": "/runtime/llm_config.toml",
                "AUTO_CODER_OWNERSHIP_FILE": "/runtime/ownership.json",
                "AUTO_CODER_ARTIFACT_DIGEST": "disposable-installed-ready",
                "AUTO_CODER_HEALTH_LOG_ENABLED": "0",
                "AUTO_CODER_DISABLE_AUTO_UPDATE": "1",
                "GITHUB_TOKEN": "nonfunctional-disposable-worker-token",
                "SSL_CERT_FILE": "/ci-trust/github.crt",
                "REQUESTS_CA_BUNDLE": "/ci-trust/github.crt",
                "NO_PROXY": "api.github.com,localhost,127.0.0.1",
                "no_proxy": "api.github.com,localhost,127.0.0.1",
            }
        )
        beta["volumes"].append(f"{Path(__file__).resolve().parents[1]}:/source:ro")
        # A worker can read only the ephemeral test CA, not the HTTPS key.
        beta["volumes"].append(f"{trust / 'github.crt'}:/ci-trust/github.crt:ro")
        runtime = self.root / "workers" / "beta"
        (runtime / "ownership.json").write_text(json.dumps({"fixture/beta": "beta"}))
        (runtime / "llm_config.toml").write_text('[backend]\norder = ["jules"]\n\n[backends.jules]\nbackend_type = "jules"\nenabled = true\napi_key = "offline-unused"\n')
        return profile

    def __init__(self, root: Path, *, installed_ready_beta: bool = False):
        self.installed_ready_beta = installed_ready_beta
        super().__init__(root, with_workers=True)
        source = root / "tested-source"
        source.mkdir()
        # The local source fixture records the checks it actually runs; it does
        # not claim the public complete-source workflow ran against this SHA.
        self.services = ExternalServices(source, "unused-synthetic-certificate")
        self.gate_phase = None
        self.gate_database = None
        self.gate_entered = threading.Event()
        self.gate_release = threading.Event()
        original_handler = self.services.handler()
        fixture = self

        class Handler(original_handler):
            def respond(self, code: int, body: object) -> None:
                # The disposable HTTP service reports actually executed local
                # fixture checks, not public GitHub Actions evidence.
                if code == 200 and isinstance(body, dict) and "/actions/runs/" in self.path:
                    if "/jobs?" in self.path and isinstance(body.get("jobs"), list):
                        run_id = int(self.path.split("/actions/runs/", 1)[1].split("/", 1)[0])
                        body = {
                            **body,
                            "jobs": [
                                {
                                    **job,
                                    "id": index + 100,
                                    "run_id": run_id,
                                    "run_attempt": 1,
                                    "completed_at": "2026-11-08T00:00:00Z",
                                }
                                for index, job in enumerate(body["jobs"])
                            ],
                        }
                    elif "/jobs?" not in self.path:
                        body = {**body, "head_branch": "main"}
                super().respond(code, body)

            def do_GET(self):
                # The beta pointer is a real direct commit ref; history and
                # tested-source refs remain immutable annotated Git tags.
                reference = "refs/tags/auto-coder-beta-current"
                if self.path == f"/repos/{fixture.external['repository']}/git/ref/tags/auto-coder-beta-current":
                    observed = subprocess.run(
                        ["git", "rev-parse", "--verify", reference],
                        cwd=fixture.services.repository,
                        capture_output=True,
                        text=True,
                    )
                    if observed.returncode:
                        self.respond(404, {})
                    else:
                        sha = observed.stdout.strip()
                        kind = subprocess.run(
                            ["git", "cat-file", "-t", sha],
                            cwd=fixture.services.repository,
                            check=True,
                            capture_output=True,
                            text=True,
                        ).stdout.strip()
                        self.respond(200, {"ref": reference, "object": {"type": kind, "sha": sha}})
                    return

                if "/actions/runs/" in self.path and fixture.gate_database is not None and fixture.gate_phase is not None:
                    try:
                        with sqlite3.connect(fixture.gate_database.as_uri() + "?mode=ro", uri=True, timeout=1) as connection:
                            phases = connection.execute("SELECT phase FROM handoffs").fetchall()
                        if (fixture.gate_phase,) in phases:
                            fixture.gate_entered.set()
                            if not fixture.gate_release.wait(60):
                                self.respond(503, {"error": "fixture boundary was not released"})
                                return
                    except sqlite3.Error:
                        pass
                super().do_GET()

        self.server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        gateway = json.loads(self.docker("network", "inspect", self.worker_network))[0]["IPAM"]["Config"][0]["Gateway"]
        self.external = {"url": f"http://{gateway}:{self.server.server_port}", "repository": "kitamura-tetsuo/auto-coder"}
        (self.oauth / "external-services.json").write_text(json.dumps(self.external))
        # Establish actual host-authority reachability from both the installer
        # and isolated child networks before producing any READY or intent.
        probe = "import sys,urllib.request;assert urllib.request.build_opener(urllib.request.ProxyHandler({})).open(sys.argv[1]+'/certificates',timeout=5).status==200;print('fixture-authority-reachable')"
        for network in ("bridge", self.worker_network):
            assert self.docker("run", "--rm", "--network", network, "--entrypoint", "python", self.image, "-c", probe, self.external["url"]).splitlines()[-1] == "fixture-authority-reachable"

        _snapshot_source(source, "source-scaffold")
        _bind_disposable_source_reader(source / "src" / "auto_coder")
        cli = source / "src" / "auto_coder" / "cli.py"
        launch_log = """    with (settings.state_root / 'installed-launches.jsonl').open('a') as observed:
        observed.write(json.dumps({'pid': os.getpid(), 'runtime': os.environ.get('AUTO_CODER_MANAGEMENT_RUNTIME_ID'), 'revision': json.loads(Path(__file__).with_name('fixture_revision.json').read_text())['revision']}) + '\\n')
"""
        needle = '    with (settings.state_root / "fixture-launch-history").open("a") as history:'
        assert cli.read_text().count(needle) == 1
        cli.write_text(cli.read_text().replace(needle, launch_log + needle))
        self.current_sha = _snapshot_source(source, "installed-initial")
        _test_source(self.services, self.current_sha, 101)
        cli = source / "src" / "auto_coder" / "cli.py"
        cli.write_text(cli.read_text().replace("class LocalCredential", "INSTALLED_REVISION = 'different-candidate-code'\n\nclass LocalCredential"))
        self.candidate_sha = _snapshot_source(source, "installed-candidate")
        _test_source(self.services, self.candidate_sha, 102)
        # Genuine annotated history and commit refs select the locally tested
        # fixture source. These records do not claim public Actions provenance.
        tested_ref = tag_name(self.current_sha)
        tested_oid = subprocess.run(
            ["git", "rev-parse", f"refs/tags/{tested_ref}"],
            cwd=source,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        selected = BetaSelectionRecord(
            1,
            self.external["repository"],
            "r101",
            self.current_sha,
            101,
            1,
            "2026-11-08T00:00:00Z",
            f"refs/tags/{tested_ref}",
            tested_oid,
            101,
            1,
            None,
        )
        history = "auto-coder-beta-selection-r101"
        subprocess.run(["git", "tag", "-a", history, "-m", selected.serialize(), self.current_sha], cwd=source, check=True, capture_output=True, text=True)
        history_oid = subprocess.run(
            ["git", "rev-parse", f"refs/tags/{history}"],
            cwd=source,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(["git", "update-ref", "refs/tags/auto-coder-beta-current", self.current_sha], cwd=source, check=True)
        subprocess.run(["git", "update-ref", "refs/tags/auto-coder-beta-selection-current", history_oid], cwd=source, check=True)
        # Constructor wiring for a private external HTTP service is a fixture
        # deployment adapter. Production stop/start and verification remain intact.
        context = root / "installed-image"
        package = context / "auto_coder"
        shutil.copytree(Path(__file__).resolve().parents[1] / "src" / "auto_coder", package, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        bootstrap = package / "management" / "bootstrap_supervisor.py"
        original = bootstrap.read_text()
        needle = "    raise SystemExit(ManagementBootstrapSupervisor(config, bootstrap_config_path=args.config, fresh_initialization=args.fresh_initialization).run())"
        assert original.count(needle) == 1
        bootstrap.write_text(original.replace(needle, _BOOTSTRAP_AUTHORITY + needle.replace("fresh_initialization=args.fresh_initialization)", "fresh_initialization=args.fresh_initialization, source_authority=authority)")))
        _bind_disposable_source_reader(package)
        self.installed_tag = "auto-coder-installed-fresh:" + uuid.uuid4().hex
        _build_fixture_image(context, self.image, self.installed_tag, owner_token_gap=False)
        self.image = self.docker("image", "inspect", self.installed_tag, "--format", "{{.Id}}")
        probe = "from pathlib import Path;from auto_coder.management.fresh_initialization import application_tree_sha256;print(application_tree_sha256(Path('/opt/auto-coder/.venv/lib/python3.12/site-packages/auto_coder')))"
        self.application_digest = self.docker("run", "--rm", "--network", "none", "--read-only", "-e", "PYTHONDONTWRITEBYTECODE=1", "--entrypoint", "python", self.image, "-c", probe).splitlines()[-1]
        self.receipts = {}

    def stage_real_ready_beta_worker(self) -> None:
        """Stage a pinned worker READY and observe its own progressing heartbeat."""
        assert self.installed_ready_beta
        source = Path(__file__).resolve().parents[1]
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=source,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        runtime = self.root / "workers" / "beta"
        # The trusted local clone spawns git-upload-pack. Unlike rev-parse,
        # that subprocess needs protected Git configuration for /source/.git.
        # Keep trust restricted to this disposable, read-only checkout.
        trusted_git = runtime / "source-safe.gitconfig"
        trusted_git.write_text("[safe]\n\tdirectory = /source\n\tdirectory = /source/.git\n")
        trusted_git.chmod(0o600)
        staged = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "bridge",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--mount",
                f"type=bind,src={source},dst=/source,readonly",
                "--mount",
                f"type=bind,src={runtime},dst=/runtime",
                "--mount",
                f"type=bind,src={Path(__file__).parent / 'fixtures'},dst=/fixtures,readonly",
                # Read only the staging fixture's narrowly scoped global Git
                # trust, including in the local-clone upload-pack subprocess.
                "-e",
                "GIT_CONFIG_GLOBAL=/runtime/source-safe.gitconfig",
                "-e",
                "HOME=/runtime/home",
                "--entrypoint",
                "python",
                self.image,
                "/fixtures/supervisor_stage_ready.py",
                revision,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=1200,
        )
        assert staged.returncode == 0, (staged.stdout[-2000:], staged.stderr[-4000:])
        prepared = json.loads(staged.stdout.splitlines()[-1])
        receipt = prepared["receipt"]
        assert receipt["source_sha"] == revision
        assert receipt["installed_runtime_id"] != "bootstrap"
        assert (runtime / Path(prepared["ready_path"]).relative_to("/runtime")).is_file()

        def manager(*arguments: str) -> dict:
            return json.loads(
                self.docker(
                    "exec",
                    self.worker_manager,
                    "python",
                    "/fixtures/supervisor_network_client_fixture.py",
                    *arguments,
                    timeout=30,
                )
            )

        stop_id = "installed-ready-stop-" + uuid.uuid4().hex
        stopped = manager("stop-worker", "beta", stop_id)
        assert stopped["result_code"] in {"DRAINING", "STOPPED"}, stopped
        (runtime / "fixture-release").write_text("release bootstrap worker after real drain\n")
        wait_for(
            lambda: manager("read-operation", "beta", stop_id)["result_code"] == "STOPPED",
            "original beta worker gracefully stopped",
            timeout=90,
        )
        started = manager(
            "start-staged",
            "beta",
            "installed-ready-start-" + uuid.uuid4().hex,
            receipt["installed_runtime_id"],
        )
        assert started["result_code"] == "RUNNING", started

        def ready_observation() -> dict | None:
            try:
                observed = manager("runtime-observe", "beta")
            except (OSError, ValueError, subprocess.CalledProcessError):
                return None
            observation = observed.get("observation")
            binding = observed.get("runtime_binding")
            heartbeat = observation.get("heartbeat") if isinstance(observation, dict) else None
            if (
                observed.get("status") == "OK"
                and observed.get("binding_status") == "VERIFIED"
                and isinstance(binding, dict)
                and binding.get("runtime_id") == receipt["installed_runtime_id"]
                and isinstance(heartbeat, dict)
                and heartbeat.get("available") is True
                and type(heartbeat.get("sequence")) is int
            ):
                return observed
            return None

        try:
            first = wait_for(ready_observation, "installed READY beta worker with producer heartbeat", timeout=120)
        except AssertionError as error:
            # Retain typed, bounded production observations for CI diagnosis.
            # Never print raw worker logs, transport bodies or credentials.
            try:
                observed = manager("runtime-observe", "beta")
                inspected = manager("inspect", "beta")
                state = inspected.get("data")
                state = state if isinstance(state, dict) else {}
                worker = state.get("worker")
                worker = worker if isinstance(worker, dict) else {}
                observation = observed.get("observation")
                observation = observation if isinstance(observation, dict) else {}
                lifecycle = observation.get("lifecycle")
                lifecycle = lifecycle if isinstance(lifecycle, dict) else {}
                heartbeat = observation.get("heartbeat")
                heartbeat = heartbeat if isinstance(heartbeat, dict) else {}
                diagnostic = {
                    "supervisor_result": inspected.get("result_code"),
                    "supervisor_state": state.get("status"),
                    "worker_alive": state.get("alive"),
                    "selected_runtime_matches": state.get("selected_runtime_id") == receipt["installed_runtime_id"],
                    "worker_runtime_matches": worker.get("runtime_id") == receipt["installed_runtime_id"],
                    "service_status": observed.get("status"),
                    "binding_status": observed.get("binding_status"),
                    "startup_reconciled": lifecycle.get("startup_reconciled"),
                    "daemon_serving": lifecycle.get("daemon_serving"),
                    "heartbeat_available": heartbeat.get("available"),
                }
            except (OSError, ValueError, subprocess.CalledProcessError) as observation_error:
                diagnostic = {"observation_error": type(observation_error).__name__}
            # Docker logs include the genuine staged child's stderr even after
            # the Supervisor loses its process lifetime. Never upload those
            # unredacted logs: the real CLI may write credentials or request
            # bodies. Report only fixed, allowlisted startup signatures.
            try:
                worker_container = self.compose("ps", "-q", "beta")
                worker_logs = self.docker("logs", "--tail", "300", worker_container, timeout=15).lower()
                startup_signatures = (
                    ("python-traceback", ("traceback (most recent call last)",)),
                    ("module-import", ("modulenotfounderror", "importerror:")),
                    ("filesystem-permission", ("permissionerror", "permission denied")),
                    ("missing-path", ("filenotfounderror", "no such file or directory")),
                    ("git-repository", ("not a git repository", "unsafe repository", "dubious ownership")),
                    ("github-tls", ("certificate_verify_failed", "sslerror", "certificate verify failed")),
                    ("github-auth", ("badcredentials", "401 unauthorized", "bad credentials")),
                    ("github-route", ("404 client error", "404 not found")),
                    ("github-network", ("connectionerror", "name or service not known", "connection refused")),
                    ("backend-prerequisite", ("ordinary backend candidate pool is empty", "backend prerequisite")),
                    ("cli-usage", ("click.exceptions.usageerror", "error: no such option")),
                    ("runtime-exception", ("runtimeerror", "keyerror", "valueerror", "typeerror")),
                )
                diagnostic["worker_log_observed"] = bool(worker_logs)
                diagnostic["worker_exception_types"] = _safe_worker_exception_types(
                    worker_logs
                )
                diagnostic["worker_startup_signatures"] = [category for category, markers in startup_signatures if any(marker in worker_logs for marker in markers)]
            except (OSError, ValueError, subprocess.CalledProcessError) as log_error:
                diagnostic["worker_log_status"] = type(log_error).__name__
            raise AssertionError(f"{error}; disposable-worker-observation={json.dumps(diagnostic, sort_keys=True)}") from error
        first_sequence = first["observation"]["heartbeat"]["sequence"]
        wait_for(
            lambda: (latest if latest["observation"]["heartbeat"]["sequence"] > first_sequence else None) if (latest := ready_observation()) is not None else None,
            "advancing producer-owned beta heartbeat",
            timeout=90,
        )
        self.worker_baseline = self.workers()
        self.worker_post_baseline = self.worker_posts()

    def configure_profile(self, name, root, config, runtime, management, mounts, *, fresh: bool):
        source = root / "bootstrap" / "source"
        shutil.copytree(self.services.repository, source, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"))
        versions = root / "versions"
        versions.mkdir(mode=0o700)
        runtime["environment"]["TMPDIR"] = "/management-child-state/logs"
        config.update(management_version_root="/management-versions", management_config="/bootstrap/management.yaml")
        values = yaml.safe_load(management.read_text())
        values.update(self_update_source_repository="/bootstrap/source", self_update_python_executable="/opt/auto-coder/.venv/bin/python", self_update_uv_executable="/usr/local/bin/uv", self_update_uv_version="0.7.22", operating_policy_path="/bootstrap/policy.yaml")
        management.write_text(yaml.safe_dump(values))
        policy = root / "bootstrap" / "policy.yaml"
        profile = yaml.safe_load(policy.read_text())
        profile["management_self_update"] = {"enabled": True, "instance_id": name, "repository": self.external["repository"]}
        policy.write_text(yaml.safe_dump(profile))
        (root / "bootstrap" / "config.json").write_text(json.dumps(config))
        # Staging runs before the bootstrap/intent is sealed. The runtime gets
        # the same versions read-only; forward never invokes the installer.
        shas = [self.current_sha] + ([self.candidate_sha] if name == "manager-c" else [])
        output = self.docker(
            "run",
            "--rm",
            "--network",
            "bridge",
            "--mount",
            f"type=bind,src={root / 'bootstrap'},dst=/bootstrap,readonly",
            "--mount",
            f"type=bind,src={self.oauth},dst=/management-oauth,readonly",
            "--mount",
            f"type=bind,src={versions},dst=/management-versions",
            "--entrypoint",
            "python",
            self.image,
            "-c",
            _STAGE,
            *shas,
            timeout=240,
        )
        prepared = json.loads(output.splitlines()[-1])
        self.receipts[name] = prepared["receipts"]
        config["runtimes"] = prepared["runtimes"]
        config["initial_runtime"] = prepared["receipts"][0]["management_runtime_id"]
        if fresh:
            ready = prepared["receipts"][0]
            config["fresh_runtime_binding"] = {
                "version": 2,
                "bootstrap_application_root": "/opt/auto-coder/.venv/lib/python3.12/site-packages/auto_coder",
                "initial_ready_path": str(Path(ready["environment_path"]).parent / "MANAGEMENT_READY.json"),
                "initial_ready_identity": ready["receipt_identity"],
            }
        mounts.append((versions, "/management-versions", False))

    def close(self):
        self.gate_release.set()
        self.services.release_ci.set()
        try:
            super().close()
        finally:
            self.server.shutdown()
            self.server.server_close()
            self.server_thread.join(10)
            assert not self.server_thread.is_alive()
            if hasattr(self, "installed_tag"):
                subprocess.run(["docker", "image", "rm", self.installed_tag], capture_output=True, timeout=30)


def wait_for(predicate, description, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("missing real installed-fresh evidence: " + description)


@pytest.fixture
def installed_ready_chain(tmp_path):
    """Opt-in installed READY worker on an exclusively disposable host."""
    if os.geteuid() != 0:
        pytest.fail("root is required for mandatory disposable Docker verification")
    fixture = InstalledChain.__new__(InstalledChain)
    try:
        fixture.__init__(tmp_path, installed_ready_beta=True)
        yield fixture
    finally:
        if hasattr(fixture, "server"):
            fixture.close()
        elif hasattr(fixture, "names"):
            Chain.close(fixture)


@pytest.fixture
def installed_chain(tmp_path):
    if os.geteuid() != 0:
        pytest.skip("host root is required for disposable namespace and proxy evidence")
    fixture = InstalledChain.__new__(InstalledChain)
    try:
        fixture.__init__(tmp_path)
        yield fixture
    finally:
        # Preserve failures during construction while still cleaning objects.
        if hasattr(fixture, "server"):
            fixture.close()
        elif hasattr(fixture, "names"):
            Chain.close(fixture)


@pytest.mark.timeout(900)
@pytest.mark.parametrize("boundary", ["complete", "before-stop", "before-forward-start", "ready-corrupt", "host-interrupted", "source-unavailable"])
def test_installed_initial_abc_real_handoff(installed_chain, boundary):
    chain = installed_chain
    a = chain.prepare("manager-a")
    chain.docker("start", a.container)

    def first_running():
        try:
            child = chain.child(a)
            return child["phase"] == "running" and child["pid"] > 0
        except (sqlite3.Error, TypeError):
            return False

    wait_for(first_running, "first actual installed RUNNING child")
    chain.authorize()
    chain.ready(a, fresh=False)
    old_a = chain.seed(a)
    workers = chain.seed_workers(a)
    b = chain.prepare("manager-b", a)
    chain.start_cutover(a, b)
    old_b = chain.seed(b)
    c = chain.prepare("manager-c", b)
    original_a = chain.snapshot(a)
    if boundary in {"host-interrupted", "source-unavailable"}:
        process = chain.host(c)
        chain.status(process, "ancestors-fenced-awaiting-manual-stop")
        chain.stop(b)
        original_b = chain.snapshot(b)
        chain.status(process, "awaiting-replacement")
        claimed = (c.path("state") / "fresh-initialization.json").read_bytes()
        if boundary == "host-interrupted":
            process.terminate()
            process.communicate(timeout=10)
        else:
            chain.services.unavailable = True
        chain.docker("start", c.container)
        assert chain.docker("wait", c.container, timeout=60) != "0"
        assert not (c.path("state") / "fresh-launch-claimed.json").exists()
        assert not (c.path("state") / "fresh-launched.json").exists()
        assert not list(c.path("journal").iterdir())
        assert (c.path("state") / "fresh-initialization.json").read_bytes() == claimed
        assert chain.snapshot(a) == original_a and chain.snapshot(b) == original_b
        if boundary == "source-unavailable":
            # The still-running host session retains all fences while the
            # replacement refuses source proof before consuming its grant.
            for old in (a, b):
                chain.owner_refused(old)
            process.terminate()
            process.communicate(timeout=10)
        assert chain.grants() == chain.grants_before
        return
    chain.start_cutover(b, c)
    original_b = chain.snapshot(b)
    before = chain.child(c)
    intent = (c.path("state") / "fresh-initialization.json").read_bytes()
    config = (c.path("bootstrap") / "config.json").read_bytes()
    launches = (chain.oauth / "fixture-launch-history").read_bytes()

    def observed_launches():
        return [json.loads(line) for line in (chain.oauth / "installed-launches.jsonl").read_text().splitlines()]

    original_launches = observed_launches()
    current, candidate = chain.receipts["manager-c"]
    assert current["source_sha"] != candidate["source_sha"]
    source_package = Path("lib/python3.12/site-packages/auto_coder")
    initial_cli = c.path("versions") / Path(current["environment_path"]).relative_to("/management-versions") / source_package / "cli.py"
    candidate_cli = c.path("versions") / Path(candidate["environment_path"]).relative_to("/management-versions") / source_package / "cli.py"
    assert initial_cli.read_bytes() != candidate_cli.read_bytes()
    receipt = c.path("versions") / Path(candidate["environment_path"]).relative_to("/management-versions").parent / "MANAGEMENT_READY.json"
    original_ready = receipt.read_bytes()
    if boundary == "ready-corrupt":
        receipt.write_bytes(b'{"schema_version":999}')
    store = HandoffStore(c.path("state"), c.name)
    if boundary in {"before-stop", "before-forward-start"}:
        chain.gate_database = store.path
        chain.gate_phase = HandoffPhase.SUBMITTED.value if boundary == "before-stop" else HandoffPhase.STOPPED.value
    response = chain.call(c, "auto_coder_update_management_service", {"source_sha": chain.candidate_sha, "idempotency_key": "installed-fresh-" + boundary})
    assert response["status"] == "ACCEPTED", response
    operation = response["operation_id"]
    saved = None
    if chain.gate_phase is not None:
        assert chain.gate_entered.wait(120), "real durable consumer did not reach its external source-proof boundary"
        saved = a.path("journal") / "management-operations.sqlite3"
        saved.rename(saved.with_suffix(".preserved"))
        chain.gate_release.set()
    try:
        if boundary == "complete":
            wait_for(lambda: chain.child(c)["selected_runtime"] == candidate["management_runtime_id"] and chain.child(c)["pid"] != before["pid"], "different installed child exec", timeout=120)
            chain.ready(c, fresh=True)

            def settled():
                try:
                    result = chain.call(c, "auto_coder_operation_get", {"operation_id": operation})
                    return result if result["status"] == "SUCCEEDED" else None
                except (ValueError, urllib.error.URLError):
                    return None

            assert wait_for(settled, "authenticated candidate success", timeout=120)["verification"]["handoff_phase"] == "succeeded"
            chain.prove_child(c)
            actual = observed_launches()[len(original_launches) :]
            assert len(actual) == 1 and actual[0]["runtime"] == candidate["management_runtime_id"]
            assert actual[0]["revision"] == "installed-candidate"
            assert store.read(operation).phase is HandoffPhase.SUCCEEDED
        elif boundary == "ready-corrupt":
            observed = wait_for(lambda: (record if record["effect_state"] == "rejected" else None) if (record := chain.call(c, "auto_coder_operation_get", {"operation_id": operation})) else None, "corrupt READY refusal")
            assert observed["status"] == "NOT_READY"
            assert store.read(operation) is None
            assert chain.child(c) == before
            assert (chain.oauth / "fixture-launch-history").read_bytes() == launches
        else:

            def refused():
                row = store.read(operation)
                return row if row is not None and row.phase in {HandoffPhase.DENIED, HandoffPhase.UNAVAILABLE, HandoffPhase.PROCESS_RESTORED, HandoffPhase.NEEDS_ATTENTION} else None

            terminal = wait_for(refused, "fail-closed durable consumer", timeout=120)
            with sqlite3.connect(c.path("state") / "management-child.sqlite3") as db:
                effects = db.execute("SELECT action,runtime_ref FROM effects WHERE operation_id LIKE ?", (operation + ":%",)).fetchall()
            assert not any(action == "START_SELECTED" for action, _ in effects)
            assert not any(runtime == candidate["management_runtime_id"] for _, runtime in effects)
            if boundary == "before-stop":
                assert chain.child(c) == before
                assert not effects
                assert (chain.oauth / "fixture-launch-history").read_bytes() == launches
            else:
                assert not any(action == "RESTORE_PREVIOUS" for action, _ in effects)
                assert terminal.phase is HandoffPhase.NEEDS_ATTENTION
                stopped = chain.child(c)
                assert stopped["phase"] == "stopped"
                assert not stopped["recovery_used"]
                assert observed_launches() == original_launches
                assert (chain.oauth / "fixture-launch-history").read_bytes() == launches
                time.sleep(1)
                assert store.read(operation) == terminal
                assert chain.child(c) == stopped
                assert observed_launches() == original_launches
                assert (chain.oauth / "fixture-launch-history").read_bytes() == launches
    finally:
        if saved is not None:
            saved.with_suffix(".preserved").rename(saved)
        if boundary == "ready-corrupt":
            receipt.write_bytes(original_ready)
    assert (c.path("state") / "fresh-initialization.json").read_bytes() == intent
    assert (c.path("bootstrap") / "config.json").read_bytes() == config
    assert chain.snapshot(a) == original_a and chain.snapshot(b) == original_b
    # No old key can create a worker request or duplicate a management effect.
    if boundary in {"complete", "ready-corrupt", "before-stop"}:
        chain.assert_workers(c, workers)
        for record in (old_a, old_b):
            replay = chain.call(c, "auto_coder_update_management_service", {"source_sha": "a" * 40, "idempotency_key": record.intent.idempotency_key})
            assert replay["status"] == "CONFLICT" if record is old_a else replay["operation_id"] == record.operation_id
    # Host shared fences protect every old owner while clean C is reconstructed.
    if boundary == "complete":
        held = []
        try:
            for old in (a, b):
                fd = os.open(old.path("state") / ".supervisor-owner.lock", os.O_RDONLY)
                held.append(fd)
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            chain.stop(c, stop_proxy=False)
            chain.docker("start", c.container)
            chain.reconnect_proxy(c)
            chain.ready(c, fresh=True)
            assert chain.child(c)["selected_runtime"] == candidate["management_runtime_id"]
            chain.prove_child(c)
            chain.assert_workers(c, workers)
        finally:
            for fd in held:
                os.close(fd)
    for old in (a, b):
        chain.owner_refused(old)
    assert chain.grants() == chain.grants_before
