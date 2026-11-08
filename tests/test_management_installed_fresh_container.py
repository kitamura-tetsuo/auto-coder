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


class InstalledChain(Chain):
    def __init__(self, root: Path):
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
        lifecycle = package / "management" / "worker_lifecycle.py"
        lifecycle_source = lifecycle.read_text()
        reader_constructor = "return BetaEvidenceReader(GitHubGitDataClient(secret, config.source_repository), config.source_repository)"
        assert lifecycle_source.count(reader_constructor) == 1
        # Substitute only the disposable external endpoint; the production
        # reader validates actual Git selection, proof and required jobs.
        lifecycle.write_text(
            lifecycle_source.replace(
                reader_constructor,
                "external = json.loads((config.oauth.database_path.parent / 'external-services.json').read_text())\n" "        return BetaEvidenceReader(GitHubGitDataClient(secret, config.source_repository, api_url=external['url']), config.source_repository)",
            )
        )
        self.installed_tag = "auto-coder-installed-fresh:" + uuid.uuid4().hex
        _build_fixture_image(context, self.image, self.installed_tag, owner_token_gap=False)
        self.image = self.docker("image", "inspect", self.installed_tag, "--format", "{{.Id}}")
        probe = "from pathlib import Path;from auto_coder.management.fresh_initialization import application_tree_sha256;print(application_tree_sha256(Path('/opt/auto-coder/.venv/lib/python3.12/site-packages/auto_coder')))"
        self.application_digest = self.docker("run", "--rm", "--network", "none", "--read-only", "-e", "PYTHONDONTWRITEBYTECODE=1", "--entrypoint", "python", self.image, "-c", probe).splitlines()[-1]
        self.receipts = {}

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
