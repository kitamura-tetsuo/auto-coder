"""Codex 0.159.2 tool execution through the production Strong Audit boundary."""

import gzip
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = [pytest.mark.opencode_live, pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")]


class ReviewProvider:
    def __init__(self, root: Path, verdict: str, write_target: str = "") -> None:
        self.requests: list[dict] = []
        self.root = root
        self.verdict = verdict
        self.write_target = write_target
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if self.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                request = json.loads(raw)
                outer.requests.append(request)
                if len(outer.requests) == 1:
                    names = [tool.get("name") for tool in request.get("tools", [])]
                    name = "exec_command" if "exec_command" in names else "shell_command" if "shell_command" in names else "shell"
                    command = 'id -u; pwd; git rev-parse HEAD; git status --short; printf "git-read-status=%s\\n" "$?"; cat AGENTS.md tracked.txt; printf runtime-ok > "$TMPDIR/read-marker"; cat "$TMPDIR/read-marker"'
                    if outer.write_target:
                        target = {"source": "tracked.txt", "git": ".git/config", "caller": str(outer.root / "caller" / "tracked.txt"), "peer": str(outer.root / "peer" / "marker.txt")}[outer.write_target]
                        command += f"; if printf forbidden > {target}; then echo WRITE_ALLOWED; else echo WRITE_DENIED; fi; cat {target}; exit 0"
                    arguments = {"cmd": command} if name == "exec_command" else {"command": command} if name == "shell_command" else {"command": ["/bin/sh", "-c", command]}
                    item = {"type": "function_call", "id": "fc_read", "call_id": "call_read", "name": name, "arguments": json.dumps(arguments)}
                else:
                    # Identity is taken from the production prompt, not invented
                    # by a fake reviewer result or a substituted client.
                    first = json.dumps(outer.requests[0])
                    first = first.replace("\\n", "\n").replace('\\"', '"')
                    identity = first.split("Identity (copy every value exactly into the JSON response):\n", 1)[1].split("Issue identities:", 1)[0]
                    payload = {}
                    for line in identity.splitlines():
                        key, sep, value = line.strip().partition("=")
                        if sep:
                            payload[key] = int(value) if key == "finding_set_revision" else value
                    payload.update(verdict=outer.verdict, findings=[])
                    if outer.verdict == "FINDINGS":
                        payload["findings"] = [
                            {
                                "finding_id": "source-finding",
                                "requirement_ids": ["#2387/REQ-001"],
                                "requirement_texts": ["Read the repository under no-edit protection."],
                                "counterexample": "The tracked source has an independently reported defect.",
                                "expected_behavior": "Preserve the reported finding.",
                                "actual_behavior": "Defect present.",
                                "evidence": "tracked.txt:1",
                                "affected_boundary": "source",
                                "is_regression_gap": False,
                                "material_consequence": "Review must not authorize merge.",
                                "focused_regression_scenario": "Retain this finding after successful reads.",
                            }
                        ]
                    if outer.verdict == "MALFORMED":
                        payload.pop("head_sha")
                    outer.tool_evidence = json.dumps([value for value in request.get("input", []) if value.get("type") in {"function_call_output", "custom_tool_call_output"}])
                    item = {"type": "message", "id": "msg_final", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": json.dumps(payload), "annotations": []}]}
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                events = [
                    {"type": "response.created", "response": {"id": "resp_test"}},
                    {"type": "response.output_item.added", "output_index": 0, "item": item},
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": {"id": "resp_test", "output": [item], "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}},
                ]
                for event in events:
                    self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())

            def log_message(self, fmt: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.tool_evidence = ""

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


@pytest.fixture
def runtime_root():
    # The worker must traverse the runtime parent; pytest's 0700 ancestors
    # are intentionally unsuitable for the production credential transition.
    with tempfile.TemporaryDirectory(prefix="codex-noedit-runtime-") as directory:
        root = Path(directory)
        root.chmod(0o755)
        yield root


@pytest.mark.parametrize("verdict,write_target", [("PASS", ""), ("FINDINGS", ""), ("INCONCLUSIVE", ""), ("MALFORMED", ""), ("PASS", "source"), ("PASS", "git"), ("PASS", "caller"), ("PASS", "peer")])
def test_real_codex_reads_and_preserves_durable_review_authority(runtime_root: Path, verdict: str, write_target: str) -> None:
    tmp_path = runtime_root
    provider = ReviewProvider(tmp_path if os.environ.get("AUTO_CODER_CODEX_HOST_RUNTIME") == "1" else Path("/tmp/codex-review-fixture"), verdict, write_target)
    home = tmp_path / "home"
    config_dir = home / ".auto-coder"
    config_dir.mkdir(parents=True)
    url = f"http://127.0.0.1:{provider.server.server_port}/v1"
    options = [
        "exec",
        "--model",
        "gpt-5-codex",
        "--json",
        "-c",
        "features.code_mode=false",
        "-c",
        'model_provider="controlled"',
        "-c",
        'model_providers.controlled.name="Controlled"',
        "-c",
        f'model_providers.controlled.base_url="{url}"',
        "-c",
        'model_providers.controlled.env_key="OPENAI_API_KEY"',
        "-c",
        'model_providers.controlled.wire_api="responses"',
        "-c",
        "features.responses_websockets=false",
    ]
    (config_dir / "llm_config.toml").write_text('[backend_strong_pr_adversarial_validation]\norder = ["renamed-reviewer"]\n' '[backends.renamed-reviewer]\nbackend_type = "codex"\nmodel = "gpt-5-codex"\nenabled = true\n' f"options_for_noedit = {json.dumps(options)}\n")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    environment = os.environ.copy()
    environment.update(HOME=str(home), CODEX_HOME=str(home / ".codex"), OPENAI_API_KEY="controlled-key", AUTO_CODER_RUNTIME_ROOT=str(runtime), AUTO_CODER_LOCAL_WORKER_UID="65532", AUTO_CODER_LOCAL_WORKER_GID="65532")
    try:
        if os.environ.get("AUTO_CODER_CODEX_HOST_RUNTIME") == "1":
            command = [sys.executable, "tests/utils/codex_review_runtime.py", str(tmp_path)]
        else:
            from tests.test_opencode_container_runtime import _container_network_args, _ensure_image_built

            image = _ensure_image_built()
            container_root = "/tmp/codex-review-fixture"
            settings = (config_dir / "llm_config.toml").read_text()
            setup = "from pathlib import Path\n" f"root = Path({container_root!r})\n" "(root / 'home' / '.auto-coder').mkdir(parents=True)\n" "(root / 'runtime').mkdir()\n" f"(root / 'home' / '.auto-coder' / 'llm_config.toml').write_text({settings!r})\n"
            driver = Path("tests/utils/codex_review_runtime.py").read_text()
            command = ["docker", "run", "--rm", "--privileged", "--cgroupns=host", "--volume", "/sys/fs/cgroup:/sys/fs/cgroup:rw", *_container_network_args()]
            for key, value in {
                "HOME": f"{container_root}/home",
                "CODEX_HOME": f"{container_root}/home/.codex",
                "AUTO_CODER_RUNTIME_ROOT": f"{container_root}/runtime",
                "OPENAI_API_KEY": "controlled-key",
                "AUTO_CODER_LOCAL_WORKER_UID": "65532",
                "AUTO_CODER_LOCAL_WORKER_GID": "65532",
            }.items():
                command.extend(["-e", f"{key}={value}"])
            command.extend(["--entrypoint", "python3", image, "-c", setup + driver, container_root])
        completed = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=180)
        output = completed.stdout + completed.stderr
        assert completed.returncode == 0, output
        result = json.loads(completed.stdout.split("RUNTIME_RESULT:", 1)[1].splitlines()[0])
        assert len(provider.requests) == 2, output
        assert "65532" in provider.tool_evidence, provider.tool_evidence
        assert result["head"] in provider.tool_evidence, provider.tool_evidence
        assert "Distinctive review guidance 2387." in provider.tool_evidence
        assert "Distinctive source head 2387." in provider.tool_evidence
        assert "runtime-ok" in provider.tool_evidence
        assert "git-read-status=0" in provider.tool_evidence
        if write_target:
            assert "WRITE_DENIED" in provider.tool_evidence
            assert "WRITE_ALLOWED" not in provider.tool_evidence
            if write_target == "source":
                assert provider.tool_evidence.count("Distinctive source head 2387.") == 2
            if write_target == "git":
                assert "repositoryformatversion = 0" in provider.tool_evidence
        else:
            assert "Permission denied" not in provider.tool_evidence
        assert result["ordinary_before"] == result["head"]
        assert result["ordinary_head"] == ("" if verdict == "FINDINGS" else result["head"])
        assert result["source"] == "Distinctive source head 2387.\n"
        accepted = verdict in {"PASS", "FINDINGS"}
        assert result["accepted"] is accepted, output
        assert result["verdict"] == (verdict if accepted else None), output
        assert result["findings"] == (["source-finding"] if verdict == "FINDINGS" else [])
        assert result["peer"] == "peer-protected\n"
        assert result["caller_git_unchanged"] is True
        assert "NESTED_SANDBOX_DIAGNOSTIC:" in output
    finally:
        provider.close()
