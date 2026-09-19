# OpenCode distributed container runtime packaging and configuration

The distributed Auto-Coder container image includes a pinned, working OpenCode CLI
release (`1.18.31` on Linux `amd64` and `arm64`) alongside required system dependencies
(`git` and `ca-certificates`). This packages local OpenCode execution into the
distributed container runtime without embedding credentials into image layers, build
arguments, metadata, or committed files.

## Pinned CLI release and image verification

The root `Dockerfile` pins the supported OpenCode CLI release to `1.18.31`:

```dockerfile
ARG OPENCODE_VERSION=1.18.31
```

During the multi-stage build, the target architecture is resolved explicitly:
- `amd64` (`x86_64`) selects `opencode-linux-x64.tar.gz`.
- `arm64` (`aarch64`) selects `opencode-linux-arm64.tar.gz`.
- Any unsupported platform fails closed immediately.

The resulting binary is installed at `/usr/local/bin/opencode` with executable
permissions, and the runtime image installs `git` and `ca-certificates` to support
repository operations and secure TLS transport to provider endpoints. The image
preserves Auto-Coder's standard entrypoint:

```bash
docker run --rm auto-coder:latest --version
docker run --rm --entrypoint opencode auto-coder:latest --version
```

Verifying `opencode --version` confirms the declared pinned release (`1.18.31`).

## Effective container HOME and storage layout

Auto-Coder deployment channels configure container execution with:
`HOME=/runtime/home`

All OpenCode paths, XDG directory paths, and Auto-Coder configuration paths are
resolved relative to this effective runtime user home rather than hardcoded `/root`
or `/home/node` paths:

| Component | Container path (`HOME=/runtime/home`) | Host path (`release` channel) | Host path (`beta` channel) |
|---|---|---|---|
| Auto-Coder LLM configuration | `/runtime/home/.auto-coder/llm_config.toml` | `./runtime/release/home/.auto-coder/llm_config.toml` | `./runtime/beta/home/.auto-coder/llm_config.toml` |
| OpenCode provider configuration | `/runtime/home/.config/opencode/opencode.jsonc` | `./runtime/release/home/.config/opencode/opencode.jsonc` | `./runtime/beta/home/.config/opencode/opencode.jsonc` |
| OpenCode authentication store | `/runtime/home/.local/share/opencode/auth.json` | `./runtime/release/home/.local/share/opencode/auth.json` | `./runtime/beta/home/.local/share/opencode/auth.json` |
| OpenCode native session DB | `/runtime/home/.local/share/opencode/opencode.db` | `./runtime/release/home/.local/share/opencode/opencode.db` | `./runtime/beta/home/.local/share/opencode/opencode.db` |
| OpenCode locks & runtime state | `/runtime/home/.local/state/opencode/` | `./runtime/release/home/.local/state/opencode/` | `./runtime/beta/home/.local/state/opencode/` |

### Channel isolation and persistence across recreation

In `compose.channels.yml`:
- The `release` container mounts `./runtime/release:/runtime` and `./runtime/workspaces/release:/workspace`.
- The `beta` container mounts `./runtime/beta:/runtime` and `./runtime/workspaces/beta:/workspace`.

Because each channel mounts its own disjoint runtime directory, their respective
writable configuration, authentication stores, and native session data are completely
isolated. Recreating a release container (`docker compose down release && docker compose up -d release`)
preserves that channel's persistent configuration, authentication store, and native session
history. Recreating beta retains beta's state without sharing or leaking into release.

Preserving native session data under `/runtime/home/.local/share/opencode/` does not
cause Auto-Coder to implicitly resume prior implementation sessions. Each new
implementation task executes as a fresh, non-interactive task (`opencode run ...`)
without `--continue` or `--session`, and Auto-Coder's backend selection remains driven
exclusively by explicit configuration (`--backend`, `llm_config.toml`, or `backend_state.json`).

## Runtime credential injection

Image layers and build metadata contain no credentials or tokens. Authentication is
configured exclusively at runtime using one of three non-interactive mechanisms:

### 1. Environment variables via Docker Compose

Set provider credentials in the container environment or in an operator `.env` file:

```bash
docker compose run -e OPENAI_API_KEY="sk-..." release auto-coder process-issues --repo owner/repo
```

Supported provider variables include `OPENCODE_API_KEY`, `OPENAI_API_KEY`,
`OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, and custom endpoint variables (`OPENAI_BASE_URL`,
`OPENROUTER_BASE_URL`).

### 2. Auto-Coder LLM configuration file

Provision the channel's `llm_config.toml` with an OpenCode alias using `tee`:

```bash
mkdir -p ./runtime/release/home/.auto-coder
tee ./runtime/release/home/.auto-coder/llm_config.toml << 'EOF'
[backend]
default = "opencode"

[backends.opencode]
backend_type = "opencode"
model = "anthropic/claude-sonnet-4-5"
api_key = "sk-ant-..."
EOF
```

When Auto-Coder invokes OpenCode, it injects configured `api_key`, `openai_api_key`, or
`openrouter_api_key` values directly into the child process environment.

### 3. OpenCode authentication store (`auth.json`)

Populate OpenCode's native authentication store file under the channel runtime mount:

```bash
mkdir -p ./runtime/release/home/.local/share/opencode
tee ./runtime/release/home/.local/share/opencode/auth.json << 'EOF'
{
  "anthropic": {
    "type": "api",
    "key": "sk-ant-..."
  },
  "openai": {
    "type": "api",
    "key": "sk-proj-..."
  }
}
EOF
chmod 600 ./runtime/release/home/.local/share/opencode/auth.json
```

If credentials are missing or invalid, OpenCode fails immediately with an actionable
error. Auto-Coder runs non-interactively and never hangs or prompts for interactive login.

## Model discovery and replacement

Each OpenCode alias must specify an explicit `provider/model` string:

```toml
[backends.fast_model]
backend_type = "opencode"
model = "anthropic/claude-3-5-haiku-20241022"

[backends.review_model]
backend_type = "opencode"
model = "anthropic/claude-sonnet-4-5"
```

To discover supported models in the container environment:

```bash
docker compose run --rm release opencode models
```

Union Alpha (e.g. `model = "union-alpha/..."`) is a replaceable reference example of a
third-party provider listing. Auto-Coder does not hardcode Union Alpha, require it as a
mandatory dependency, or guarantee its pricing or availability. Operators may substitute
any provider/model supported by OpenCode or configured in `opencode.jsonc`.

## Preservation of existing contracts

Packaging OpenCode does not change:
- The default entrypoint (`ENTRYPOINT ["auto-coder"]`).
- The default backend (remains `codex` unless explicitly changed).
- Deployment repository ownership and channel separation in `compose.channels.yml`.
- System behavior when OpenCode is not configured: runs configured for other backends
  make zero OpenCode network requests, require no OpenCode credentials, and start no
  background OpenCode services.

## Testing and CI verification

Testing for the OpenCode container runtime is divided into two distinct tiers:

### 1. Ordinary static and contract tests

Static assertions that verify Dockerfile pinning, Compose channel mount isolation, path resolution helpers, and documentation contracts are ordinary pytest tests. They execute as part of standard PR test shards (`PR Tests` workflow) and do not require a Docker daemon or OpenCode CLI installation.

Run ordinary static tests locally:
```bash
pytest -m "not browser and not opencode_live"
```

### 2. Dedicated live container runtime scenarios

The assertion-bearing container execution scenarios (`AC-001` through `AC-005`) launch real Docker containers and execute tasks against controlled local providers. To prevent expensive container builds and multi-minute test execution from causing timeouts or requiring retry-based cache warming in ordinary PR test shards, these scenarios are classified with `@pytest.mark.opencode_live`.

In CI, the dedicated `OpenCode Live Tests` workflow (`.github/workflows/opencode-live-tests.yml`) executes these scenarios. It preflights Docker, builds the production runtime image for the exact checked-out commit via `scripts/prepare_opencode_image.py` (with a 10-minute step limit), and runs the live suite (with a 20-minute step limit and a 40-minute job limit). All five container scenarios must execute to passing results; skipping, xfailing, or missing container scenarios fails the run closed.

To execute the live container suite locally:
```bash
python scripts/prepare_opencode_image.py
pytest -m opencode_live -vv
```
