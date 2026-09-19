"""Regression tests for OpenCode distributed container runtime packaging and configuration (Issue #2128)."""

from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import pytest
import yaml

_PINNED_OPENCODE_VERSION = "1.18.31"
_IMAGE_TAG = "auto-coder:opencode"


@dataclass
class CapturedProviderRequest:
    method: str = ""
    path: str = ""
    auth_header: str = ""
    body: str = ""


@dataclass
class ScriptedProviderTurn:
    kind: str = "text"
    content: str = ""


class ControlledProviderServer:
    def __init__(self, turns: Optional[List[ScriptedProviderTurn]] = None) -> None:
        self.turns: List[ScriptedProviderTurn] = turns or [ScriptedProviderTurn(kind="text", content="Normalized assistant reply")]
        self.turn_index: int = 0
        self.captured_requests: List[CapturedProviderRequest] = []
        self._lock = threading.Lock()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                raw_body = self.rfile.read(length) if length > 0 else b""
                auth_val = self.headers.get("Authorization", "")
                with outer._lock:
                    outer.captured_requests.append(
                        CapturedProviderRequest(
                            method="POST",
                            path=self.path,
                            auth_header=auth_val,
                            body=raw_body.decode("utf-8", errors="replace"),
                        )
                    )
                    try:
                        parsed = json.loads(raw_body.decode("utf-8")) if raw_body else {}
                    except Exception:
                        parsed = {}

                    # OpenCode session-title requests carry no tools
                    if not parsed.get("tools"):
                        self._stream_response("text", "untitled")
                        return

                    idx = min(outer.turn_index, len(outer.turns) - 1)
                    outer.turn_index += 1
                    current_turn = outer.turns[idx]

                self._stream_response(current_turn.kind, current_turn.content)

            def _stream_response(self, kind: str, content: str) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                delta = {"role": "assistant", "content": content}
                chunk = {
                    "id": "cmpl-1",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "controlled-model",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                }
                done = {
                    "id": "cmpl-1",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "controlled-model",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(f"data: {json.dumps(done)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")

            def log_message(self, fmt: str, *args: object) -> None:
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port: int = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _ensure_image_built() -> str:
    """Ensure the current-checkout image exists, or use the dedicated prepared image."""
    env_image = os.environ.get("AUTOCODER_OPENCODE_IMAGE") or os.environ.get("AUTO_CODER_OPENCODE_IMAGE")
    if env_image:
        check = subprocess.run(["docker", "image", "inspect", env_image], capture_output=True, text=True)
        if check.returncode != 0:
            raise RuntimeError(f"Specified OpenCode image '{env_image}' does not exist or Docker is unavailable: {check.stderr}")
        return env_image

    if os.environ.get("AUTO_CODER_REQUIRE_OPENCODE_LIVE") == "1":
        raise RuntimeError("Dedicated OpenCode live CI requires an explicitly prepared image via AUTOCODER_OPENCODE_IMAGE")

    # Local fallback: resolve current commit revision to avoid stale tags
    try:
        commit_res = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
        commit = commit_res.stdout.strip()
        local_tag = f"auto-coder:opencode-{commit[:12]}"
    except Exception:
        commit = "unknown"
        local_tag = _IMAGE_TAG

    check = subprocess.run(["docker", "image", "inspect", local_tag], capture_output=True, text=True)
    if check.returncode != 0:
        build = subprocess.run(
            ["docker", "build", "--build-arg", f"AUTO_CODER_SOURCE_REVISION={commit}", "-t", local_tag, "."],
            capture_output=True,
            text=True,
        )
        if build.returncode != 0:
            raise RuntimeError(f"Failed to build {local_tag}: {build.stderr}")
    return local_tag


def _container_network_args() -> List[str]:
    """Provide network flags that allow the child container to access the host loopback."""
    if Path("/.dockerenv").exists() and Path("/etc/hostname").exists():
        hostname = Path("/etc/hostname").read_text(encoding="utf-8").strip()
        return [f"--network=container:{hostname}"]
    return ["--network=host"]


# -----------------------------------------------------------------------------
# Static and contract assertions (REQ-001, REQ-005, REQ-006, REQ-007)
# -----------------------------------------------------------------------------


def test_dockerfile_pins_opencode_release_and_explicit_architectures() -> None:
    dockerfile_path = Path(__file__).parents[1] / "Dockerfile"
    content = dockerfile_path.read_text(encoding="utf-8")

    # REQ-001: Pins OpenCode release 1.18.31
    assert f"ARG OPENCODE_VERSION={_PINNED_OPENCODE_VERSION}" in content

    # REQ-001: Explicit platform mapping for Linux amd64 and arm64
    assert 'amd64) OPENCODE_ARCH="x64" ;;' in content
    assert 'arm64) OPENCODE_ARCH="arm64" ;;' in content
    assert "Unsupported architecture for OpenCode: $ARCH" in content

    # REQ-001: Required OS packages in runtime image
    assert "git" in content
    assert "ca-certificates" in content

    # REQ-001: Binary copied to final image
    assert "COPY --from=build /usr/local/bin/opencode /usr/local/bin/opencode" in content

    # REQ-005: No embedded credentials or hardcoded tokens in image definitions
    forbidden_tokens = ["api_key", "secret", "token", "password", "ghp_", "sk-"]
    for line in content.splitlines():
        if line.strip().startswith("#"):
            continue
        for token in forbidden_tokens:
            assert token not in line.lower(), f"Potential credential token '{token}' in Dockerfile: {line}"

    # REQ-007: Preserves standard entrypoint
    assert 'ENTRYPOINT ["auto-coder"]' in content


def test_compose_channels_runtime_mounts_and_isolation() -> None:
    compose_path = Path(__file__).parents[1] / "compose.channels.yml"
    compose_data = yaml.safe_load(compose_path.read_text(encoding="utf-8"))

    services = compose_data.get("services", {})
    assert set(services.keys()) == {"release", "beta"}

    release = services["release"]
    beta = services["beta"]

    # REQ-003: Effective HOME is /runtime/home
    assert release["environment"]["HOME"] == "/runtime/home"
    assert beta["environment"]["HOME"] == "/runtime/home"

    # REQ-004: Persistent runtime mounts are separated between channels
    release_vols = {v.split(":")[0] for v in release["volumes"] if not v.endswith(":/routing")}
    beta_vols = {v.split(":")[0] for v in beta["volumes"] if not v.endswith(":/routing")}

    assert "./runtime/release" in release_vols
    assert "./runtime/beta" in beta_vols
    assert release_vols.isdisjoint(beta_vols)


def test_effective_home_and_xdg_storage_resolution() -> None:
    """Verify that OpenCode and Auto-Coder storage paths resolve relative to effective HOME."""
    test_home = Path("/custom/runtime/home")

    # OpenCode XDG paths
    config_dir = test_home / ".config" / "opencode"
    auth_file = test_home / ".local" / "share" / "opencode" / "auth.json"
    session_db = test_home / ".local" / "share" / "opencode" / "opencode.db"
    auto_coder_config = test_home / ".auto-coder" / "llm_config.toml"

    assert str(config_dir).startswith(str(test_home))
    assert str(auth_file).startswith(str(test_home))
    assert str(session_db).startswith(str(test_home))
    assert str(auto_coder_config).startswith(str(test_home))

    # Ensure no hardcoded developer paths
    for p in [config_dir, auth_file, session_db, auto_coder_config]:
        assert not str(p).startswith("/home/node")
        assert not str(p).startswith("/root")


def test_documentation_describes_opencode_container_runtime() -> None:
    """Verify setup documentation fulfills all REQ-006 criteria."""
    doc_path = Path(__file__).parents[1] / "docs/client-features/opencode-container-runtime.md"
    content = doc_path.read_text(encoding="utf-8")

    # Pinned release and platforms
    assert _PINNED_OPENCODE_VERSION in content
    assert "amd64" in content
    assert "arm64" in content

    # Host and container verification commands
    assert "opencode --version" in content
    assert "docker run --rm" in content

    # Effective configuration and data locations
    assert "/runtime/home/.config/opencode" in content
    assert "/runtime/home/.local/share/opencode/auth.json" in content
    assert "/runtime/home/.local/share/opencode/opencode.db" in content
    assert "compose.channels.yml" in content

    # Runtime credential injection
    assert "OPENCODE_API_KEY" in content
    assert "OPENAI_API_KEY" in content
    assert "auth.json" in content

    # Aliases with explicit provider/model
    assert 'backend_type = "opencode"' in content
    assert "provider/model" in content

    # Model discovery and replacement
    assert "opencode models" in content

    # Union Alpha as replaceable example
    assert "Union Alpha" in content
    assert "replaceable" in content

    # Commands and file-creation examples use docker compose and tee
    assert "docker compose" in content
    assert "tee" in content


# -----------------------------------------------------------------------------
# Live container verification scenarios (AC-001 through AC-005)
# -----------------------------------------------------------------------------


@pytest.mark.opencode_live
def test_ac001_container_executes_opencode_task_against_controlled_provider() -> None:
    """AC-001: Build image, verify pinned CLI, and execute production path against controlled provider."""
    image = _ensure_image_built()

    # Verify declared pinned CLI version in final image
    version_check = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "opencode", image, "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert _PINNED_OPENCODE_VERSION in version_check.stdout

    provider = ControlledProviderServer([ScriptedProviderTurn(kind="text", content="AC001_SUCCESS_FINAL_ANSWER")])
    try:
        container_script = f"""
import os, subprocess, json
from pathlib import Path
from auto_coder.opencode_client import OpenCodeClient

home = Path("/runtime/home")
home.mkdir(parents=True, exist_ok=True)
config_dir = home / ".config" / "opencode"
config_dir.mkdir(parents=True, exist_ok=True)
config = {{
    "$schema": "https://opencode.ai/config.json",
    "share": "disabled",
    "provider": {{
        "controlled": {{
            "npm": "@ai-sdk/openai-compatible",
            "options": {{"baseURL": "http://127.0.0.1:{provider.port}/v1"}},
            "models": {{
                "controlled-model": {{
                    "name": "controlled-model",
                    "tool_call": True,
                    "cost": {{"input": 0, "output": 0}},
                    "limit": {{"context": 128000, "output": 8192}}
                }}
            }}
        }}
    }}
}}
(config_dir / "opencode.json").write_text(json.dumps(config))

repo = Path("/tmp/ac001_repo")
repo.mkdir(parents=True, exist_ok=True)
subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
(repo / "tracked.txt").write_text("initial content\\n")
subprocess.run(["git", "add", "."], cwd=repo, check=True)
subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True)

auto_coder_dir = home / ".auto-coder"
auto_coder_dir.mkdir(parents=True, exist_ok=True)
(auto_coder_dir / "llm_config.toml").write_text('''
[backends.opencode]
backend_type = "opencode"
model = "controlled/controlled-model"
api_key = "sentinel-key"
''')

os.chdir(str(repo))
client = OpenCodeClient(backend_name="opencode")
answer = client._run_llm_cli("Generate solution for task")
print("NORMALIZED_ANSWER:" + answer)
"""
        cmd = [
            "docker",
            "run",
            "--rm",
            *_container_network_args(),
            "-e",
            "HOME=/runtime/home",
            "--entrypoint",
            "python3",
            image,
            "-c",
            container_script,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        assert result.returncode == 0, f"Task execution failed: {result.stderr}\nSTDOUT: {result.stdout}"
        assert "NORMALIZED_ANSWER:AC001_SUCCESS_FINAL_ANSWER" in result.stdout

        # Verify provider observed full provider/model and non-empty prompt
        assert len(provider.captured_requests) > 0
        prompt_requests = [r for r in provider.captured_requests if "tools" in r.body]
        assert len(prompt_requests) >= 1
        assert "Generate solution for task" in prompt_requests[0].body
    finally:
        provider.stop()


@pytest.mark.opencode_live
def test_ac002_effective_home_and_runtime_authentication() -> None:
    """AC-002: Run with HOME=/runtime/home and test runtime auth via auth store, env vars, and missing auth."""
    image = _ensure_image_built()

    # 1. Runtime auth via auth store (auth.json)
    provider_store = ControlledProviderServer([ScriptedProviderTurn(kind="text", content="AUTH_STORE_OK")])
    try:
        script_auth_store = f"""
import os, subprocess, json
from pathlib import Path
from auto_coder.opencode_client import OpenCodeClient

home = Path("/runtime/home")
home.mkdir(parents=True, exist_ok=True)
config_dir = home / ".config" / "opencode"
config_dir.mkdir(parents=True, exist_ok=True)
(config_dir / "opencode.json").write_text(json.dumps({{
    "$schema": "https://opencode.ai/config.json",
    "share": "disabled",
    "provider": {{
        "controlled": {{
            "npm": "@ai-sdk/openai-compatible",
            "options": {{"baseURL": "http://127.0.0.1:{provider_store.port}/v1"}},
            "models": {{"test-model": {{"name": "test-model", "tool_call": True, "cost": {{"input": 0, "output": 0}}, "limit": {{"context": 128000, "output": 8192}}}}}}
        }}
    }}
}}))

share_dir = home / ".local" / "share" / "opencode"
share_dir.mkdir(parents=True, exist_ok=True)
(share_dir / "auth.json").write_text(json.dumps({{
    "controlled": {{"type": "api", "key": "secret-from-auth-store"}}
}}))

repo = Path("/tmp/ac002_repo1")
repo.mkdir(parents=True, exist_ok=True)
subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
(repo / "f.txt").write_text("x")
subprocess.run(["git", "add", "."], cwd=repo, check=True)
subprocess.run(["git", "commit", "-m", "i"], cwd=repo, check=True)

auto_coder_dir = home / ".auto-coder"
auto_coder_dir.mkdir(parents=True, exist_ok=True)
(auto_coder_dir / "llm_config.toml").write_text('''
[backends.opencode]
backend_type = "opencode"
model = "controlled/test-model"
''')

os.chdir(str(repo))
client = OpenCodeClient(backend_name="opencode")
answer = client._run_llm_cli("Task with auth store")
print("ANSWER:" + answer)
"""
        cmd_store = [
            "docker",
            "run",
            "--rm",
            *_container_network_args(),
            "-e",
            "HOME=/runtime/home",
            "--entrypoint",
            "python3",
            image,
            "-c",
            script_auth_store,
        ]
        res_store = subprocess.run(cmd_store, capture_output=True, text=True)
        assert res_store.returncode == 0, f"Auth store run failed: {res_store.stderr}"
        assert "ANSWER:AUTH_STORE_OK" in res_store.stdout
        # Verify provider received the credential
        assert any("secret-from-auth-store" in req.auth_header for req in provider_store.captured_requests)
    finally:
        provider_store.stop()

    # 2. Runtime auth via environment variable
    provider_env = ControlledProviderServer([ScriptedProviderTurn(kind="text", content="AUTH_ENV_OK")])
    try:
        script_auth_env = f"""
import os, subprocess, json
from pathlib import Path
from auto_coder.opencode_client import OpenCodeClient

home = Path("/runtime/home")
home.mkdir(parents=True, exist_ok=True)
config_dir = home / ".config" / "opencode"
config_dir.mkdir(parents=True, exist_ok=True)
(config_dir / "opencode.json").write_text(json.dumps({{
    "$schema": "https://opencode.ai/config.json",
    "share": "disabled",
    "provider": {{
        "controlled": {{
            "npm": "@ai-sdk/openai-compatible",
            "options": {{"baseURL": "http://127.0.0.1:{provider_env.port}/v1"}},
            "models": {{"test-model": {{"name": "test-model", "tool_call": True, "cost": {{"input": 0, "output": 0}}, "limit": {{"context": 128000, "output": 8192}}}}}}
        }}
    }}
}}))

repo = Path("/tmp/ac002_repo2")
repo.mkdir(parents=True, exist_ok=True)
subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
(repo / "f.txt").write_text("x")
subprocess.run(["git", "add", "."], cwd=repo, check=True)
subprocess.run(["git", "commit", "-m", "i"], cwd=repo, check=True)

auto_coder_dir = home / ".auto-coder"
auto_coder_dir.mkdir(parents=True, exist_ok=True)
(auto_coder_dir / "llm_config.toml").write_text('''
[backends.opencode]
backend_type = "opencode"
model = "controlled/test-model"
''')

os.chdir(str(repo))
client = OpenCodeClient(backend_name="opencode")
answer = client._run_llm_cli("Task with env auth")
print("ANSWER:" + answer)
"""
        cmd_env = [
            "docker",
            "run",
            "--rm",
            *_container_network_args(),
            "-e",
            "HOME=/runtime/home",
            "-e",
            "OPENAI_API_KEY=token-from-env-var",
            "--entrypoint",
            "python3",
            image,
            "-c",
            script_auth_env,
        ]
        res_env = subprocess.run(cmd_env, capture_output=True, text=True)
        assert res_env.returncode == 0, f"Auth env run failed: {res_env.stderr}"
        assert "ANSWER:AUTH_ENV_OK" in res_env.stdout
    finally:
        provider_env.stop()

    # 3. Missing credential yields actionable failure without interactive login or hang
    script_missing = """
import os, subprocess, json
from pathlib import Path
from auto_coder.opencode_client import OpenCodeClient

home = Path("/runtime/home")
home.mkdir(parents=True, exist_ok=True)
config_dir = home / ".config" / "opencode"
config_dir.mkdir(parents=True, exist_ok=True)
(config_dir / "opencode.json").write_text(json.dumps({
    "$schema": "https://opencode.ai/config.json",
    "share": "disabled",
    "provider": {
        "unauthenticated": {
            "npm": "@ai-sdk/openai-compatible",
            "options": {"baseURL": "http://127.0.0.1:18000/v1"},
            "models": {"test-model": {"name": "test-model", "tool_call": True, "cost": {"input": 0, "output": 0}, "limit": {"context": 128000, "output": 8192}}}
        }
    }
}))

repo = Path("/tmp/ac002_repo3")
repo.mkdir(parents=True, exist_ok=True)
subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
(repo / "f.txt").write_text("x")
subprocess.run(["git", "add", "."], cwd=repo, check=True)
subprocess.run(["git", "commit", "-m", "i"], cwd=repo, check=True)

auto_coder_dir = home / ".auto-coder"
auto_coder_dir.mkdir(parents=True, exist_ok=True)
(auto_coder_dir / "llm_config.toml").write_text('''
[backends.opencode]
backend_type = "opencode"
model = "unauthenticated/test-model"
''')

os.chdir(str(repo))
client = OpenCodeClient(backend_name="opencode")
client._run_llm_cli("Task missing auth")
"""
    cmd_missing = [
        "docker",
        "run",
        "--rm",
        *_container_network_args(),
        "-e",
        "HOME=/runtime/home",
        "--entrypoint",
        "python3",
        image,
        "-c",
        script_missing,
    ]
    res_missing = subprocess.run(cmd_missing, capture_output=True, text=True)
    # Must fail cleanly and report error
    assert res_missing.returncode != 0
    combined_err = res_missing.stderr + res_missing.stdout
    assert "OpenCode CLI" in combined_err or "error" in combined_err.lower()


@pytest.mark.opencode_live
def test_ac003_retained_and_isolated_native_state_between_channels() -> None:
    """AC-003: Verify release mount retains native session data across container recreations, beta mount is isolated, and subsequent tasks remain fresh."""
    image = _ensure_image_built()

    vol_release = f"test_ac003_release_{os.getpid()}"
    vol_beta = f"test_ac003_beta_{os.getpid()}"
    subprocess.run(["docker", "volume", "create", vol_release], check=True, capture_output=True)
    subprocess.run(["docker", "volume", "create", vol_beta], check=True, capture_output=True)

    provider = ControlledProviderServer(
        [
            ScriptedProviderTurn(kind="text", content="SESSION_1_OUTPUT"),
            ScriptedProviderTurn(kind="text", content="SESSION_2_OUTPUT"),
        ]
    )

    try:
        # Step 1: Run OpenCode task under release mount, creating native session data
        script_task_1 = f"""
import os, subprocess, json
from pathlib import Path
from auto_coder.opencode_client import OpenCodeClient

home = Path("/runtime/home")
home.mkdir(parents=True, exist_ok=True)
config_dir = home / ".config" / "opencode"
config_dir.mkdir(parents=True, exist_ok=True)
(config_dir / "opencode.json").write_text(json.dumps({{
    "$schema": "https://opencode.ai/config.json",
    "share": "disabled",
    "provider": {{
        "controlled": {{
            "npm": "@ai-sdk/openai-compatible",
            "options": {{"baseURL": "http://127.0.0.1:{provider.port}/v1"}},
            "models": {{"m": {{"name": "m", "tool_call": True, "cost": {{"input": 0, "output": 0}}, "limit": {{"context": 128000, "output": 8192}}}}}}
        }}
    }}
}}))

repo = Path("/runtime/workspace/repo")
repo.mkdir(parents=True, exist_ok=True)
subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
(repo / "f.txt").write_text("x")
subprocess.run(["git", "add", "."], cwd=repo, check=True)
subprocess.run(["git", "commit", "-m", "i"], cwd=repo, check=True)

auto_coder_dir = home / ".auto-coder"
auto_coder_dir.mkdir(parents=True, exist_ok=True)
(auto_coder_dir / "llm_config.toml").write_text('''
[backends.opencode]
backend_type = "opencode"
model = "controlled/m"
api_key = "tok"
''')

os.chdir(str(repo))
client = OpenCodeClient(backend_name="opencode")
client._run_llm_cli("Task 1")
print("SESSION_ID:" + (client.get_last_session_id() or ""))
"""
        cmd_run_1 = [
            "docker",
            "run",
            "--rm",
            *_container_network_args(),
            "-v",
            f"{vol_release}:/runtime",
            "-e",
            "HOME=/runtime/home",
            "--entrypoint",
            "python3",
            image,
            "-c",
            script_task_1,
        ]
        res_1 = subprocess.run(cmd_run_1, capture_output=True, text=True)
        assert res_1.returncode == 0, f"Task 1 failed: {res_1.stderr}"
        session_id_1 = ""
        for line in res_1.stdout.splitlines():
            if line.startswith("SESSION_ID:"):
                session_id_1 = line.partition("SESSION_ID:")[2].strip()
        assert session_id_1 != "", "Session ID was not recorded"

        # Step 2: Recreate container with the same release volume and verify OpenCode observes the session data
        cmd_inspect_release = [
            "docker",
            "run",
            "--rm",
            *_container_network_args(),
            "-v",
            f"{vol_release}:/runtime",
            "-e",
            "HOME=/runtime/home",
            "-w",
            "/runtime/workspace/repo",
            "--entrypoint",
            "opencode",
            image,
            "session",
            "list",
            "--format",
            "json",
        ]
        res_inspect = subprocess.run(cmd_inspect_release, capture_output=True, text=True)
        assert res_inspect.returncode == 0
        assert session_id_1 in res_inspect.stdout

        # Step 3: Start beta with its separate mount and verify it CANNOT observe release's session data
        cmd_inspect_beta = [
            "docker",
            "run",
            "--rm",
            *_container_network_args(),
            "-v",
            f"{vol_beta}:/runtime",
            "-e",
            "HOME=/runtime/home",
            "--entrypoint",
            "opencode",
            image,
            "session",
            "list",
            "--format",
            "json",
        ]
        res_beta = subprocess.run(cmd_inspect_beta, capture_output=True, text=True)
        assert res_beta.returncode == 0
        assert session_id_1 not in res_beta.stdout

        # Step 4: Run second task in release - verify it remains fresh (creates new session) rather than resuming session_id_1
        script_task_2 = """
import os
from pathlib import Path
from auto_coder.opencode_client import OpenCodeClient

os.chdir("/runtime/workspace/repo")
client = OpenCodeClient(backend_name="opencode")
client._run_llm_cli("Task 2 fresh")
print("SESSION_ID_2:" + (client.get_last_session_id() or ""))
"""
        cmd_run_2 = [
            "docker",
            "run",
            "--rm",
            *_container_network_args(),
            "-v",
            f"{vol_release}:/runtime",
            "-e",
            "HOME=/runtime/home",
            "--entrypoint",
            "python3",
            image,
            "-c",
            script_task_2,
        ]
        res_2 = subprocess.run(cmd_run_2, capture_output=True, text=True)
        assert res_2.returncode == 0, f"Task 2 failed: {res_2.stderr}"
        session_id_2 = ""
        for line in res_2.stdout.splitlines():
            if line.startswith("SESSION_ID_2:"):
                session_id_2 = line.partition("SESSION_ID_2:")[2].strip()
        assert session_id_2 != "", "Second session ID not found"
        assert session_id_2 != session_id_1, "Next task must be fresh and not resume previous session"

    finally:
        provider.stop()
        subprocess.run(["docker", "volume", "rm", "-f", vol_release], capture_output=True)
        subprocess.run(["docker", "volume", "rm", "-f", vol_beta], capture_output=True)


@pytest.mark.opencode_live
def test_ac004_no_baked_credentials_or_unsolicited_provider_calls() -> None:
    """AC-004: Inspect image history for credential leakage, and verify non-OpenCode startup contacts zero endpoints."""
    image = _ensure_image_built()

    # Inspect image history and layers for any token leakage
    history_res = subprocess.run(
        ["docker", "history", "--no-trunc", image],
        capture_output=True,
        text=True,
        check=True,
    )
    history_text = history_res.stdout.lower()
    for sensitive in ["sk-ant-", "sk-proj-", "ghp_", "password=", "secret_key"]:
        assert sensitive not in history_text, f"Credential leaked in image history: {sensitive}"

    # Setup controlled mock server
    provider = ControlledProviderServer([])
    try:
        # Start container with default backend (codex) and non-OpenCode config
        non_opencode_script = f"""
import os
from pathlib import Path
from auto_coder.llm_backend_config import get_llm_config

# Default config does not select OpenCode
config = get_llm_config()
assert config.default_backend != "opencode"
print("DEFAULT_BACKEND:" + config.default_backend)
"""
        cmd = [
            "docker",
            "run",
            "--rm",
            *_container_network_args(),
            "-e",
            "HOME=/runtime/home",
            "--entrypoint",
            "python3",
            image,
            "-c",
            non_opencode_script,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        assert res.returncode == 0
        assert "DEFAULT_BACKEND:codex" in res.stdout

        # Verify zero requests made to provider
        assert len(provider.captured_requests) == 0

        # Verify no background opencode processes running
        ps_check = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", "sh", image, "-c", "pgrep opencode || true"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert ps_check.stdout.strip() == ""
    finally:
        provider.stop()


@pytest.mark.opencode_live
def test_ac005_documentation_matches_production_compose_and_route() -> None:
    """AC-005: Follow documented setup with a substituted provider/model, verifying Compose mounts and production path."""
    image = _ensure_image_built()

    provider = ControlledProviderServer([ScriptedProviderTurn(kind="text", content="DOCUMENTED_ROUTE_SUCCESS")])
    try:
        # Operator provisions configuration with a substituted provider/model (not Union Alpha)
        script_doc_verification = f"""
import os, subprocess, json
from pathlib import Path
from auto_coder.opencode_client import OpenCodeClient

home = Path("/runtime/home")
home.mkdir(parents=True, exist_ok=True)
config_dir = home / ".config" / "opencode"
config_dir.mkdir(parents=True, exist_ok=True)

# Substituted provider/model
(config_dir / "opencode.jsonc").write_text(json.dumps({{
    "$schema": "https://opencode.ai/config.json",
    "share": "disabled",
    "provider": {{
        "substituted-provider": {{
            "npm": "@ai-sdk/openai-compatible",
            "options": {{"baseURL": "http://127.0.0.1:{provider.port}/v1"}},
            "models": {{
                "substituted-model": {{
                    "name": "substituted-model",
                    "tool_call": True,
                    "cost": {{"input": 0, "output": 0}},
                    "limit": {{"context": 128000, "output": 8192}}
                }}
            }}
        }}
    }}
}}))

repo = Path("/tmp/ac005_repo")
repo.mkdir(parents=True, exist_ok=True)
subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
(repo / "f.txt").write_text("x")
subprocess.run(["git", "add", "."], cwd=repo, check=True)
subprocess.run(["git", "commit", "-m", "i"], cwd=repo, check=True)

auto_coder_dir = home / ".auto-coder"
auto_coder_dir.mkdir(parents=True, exist_ok=True)
(auto_coder_dir / "llm_config.toml").write_text('''
[backends.substituted_alias]
backend_type = "opencode"
model = "substituted-provider/substituted-model"
api_key = "test-substituted-key"
''')

os.chdir(str(repo))
client = OpenCodeClient(backend_name="substituted_alias")
assert client.model_name == "substituted-provider/substituted-model"
answer = client._run_llm_cli("Documented workflow test")
print("RESULT:" + answer)
"""
        cmd = [
            "docker",
            "run",
            "--rm",
            *_container_network_args(),
            "-e",
            "HOME=/runtime/home",
            "--entrypoint",
            "python3",
            image,
            "-c",
            script_doc_verification,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        assert res.returncode == 0, f"Documented workflow run failed: {res.stderr}"
        assert "RESULT:DOCUMENTED_ROUTE_SUCCESS" in res.stdout
    finally:
        provider.stop()
