FROM python:3.12-slim AS python-environment
ARG AUTO_CODER_BUILD_IDENTITY_B64
COPY --from=ghcr.io/astral-sh/uv:0.7.22 /uv /usr/local/bin/uv
WORKDIR /opt/auto-coder
COPY pyproject.toml uv.lock README.md LICENSE MANIFEST.in ./
COPY build_backend ./build_backend
COPY src ./src
RUN test -n "$AUTO_CODER_BUILD_IDENTITY_B64" && echo "$AUTO_CODER_BUILD_IDENTITY_B64" | base64 -d > .auto-coder-build-identity.json && \
    AUTO_CODER_BUILD_IDENTITY_FILE=/opt/auto-coder/.auto-coder-build-identity.json \
    uv sync --locked --no-dev --no-editable --no-cache

FROM node:24-slim AS build
ARG TARGETARCH
ARG OPENCODE_VERSION=1.18.31
ARG CODEX_VERSION=0.159.2
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && \
    ARCH="${TARGETARCH:-$(dpkg --print-architecture)}" && \
    case "$ARCH" in \
        amd64) OPENCODE_ARCH="x64"; CODEX_ARCH="x86_64" ;; \
        arm64) OPENCODE_ARCH="arm64"; CODEX_ARCH="aarch64" ;; \
        *) echo "Unsupported architecture for OpenCode: $ARCH" >&2; exit 1 ;; \
    esac && \
    npm install --prefix /tmp/opencode --ignore-scripts --no-audit --no-fund --no-save "opencode-linux-${OPENCODE_ARCH}@${OPENCODE_VERSION}" && \
    install -m 0755 "/tmp/opencode/node_modules/opencode-linux-${OPENCODE_ARCH}/bin/opencode" /usr/local/bin/opencode && \
    test "$(/usr/local/bin/opencode --version)" = "$OPENCODE_VERSION" && \
    npm install --prefix /tmp/codex --ignore-scripts --no-audit --no-fund --no-save "@openai/codex-linux-${OPENCODE_ARCH}@npm:@openai/codex@${CODEX_VERSION}-linux-${OPENCODE_ARCH}" && \
    install -m 0755 "/tmp/codex/node_modules/@openai/codex-linux-${OPENCODE_ARCH}/vendor/${CODEX_ARCH}-unknown-linux-musl/bin/codex" /usr/local/bin/codex && \
    test "$(/usr/local/bin/codex --version)" = "codex-cli $CODEX_VERSION" && \
    rm -rf /tmp/opencode /tmp/codex && \
    rm -rf /var/lib/apt/lists/*

FROM python:3.12-slim
ARG AUTO_CODER_SOURCE_REVISION=unknown
LABEL org.opencontainers.image.revision=$AUTO_CODER_SOURCE_REVISION
# Runtime revision staging uses this immutable bootstrap tool; it never updates
# uv or the system Python environment from inside the running worker.
COPY --from=ghcr.io/astral-sh/uv:0.7.22 /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates curl && \
    MUSE_INSTALL_DIR=/usr/local/bin curl -fsSL https://dev.meta.ai/install.sh | bash && \
    apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
RUN groupadd --gid 65532 auto-coder-worker && useradd --uid 65532 --gid 65532 --no-create-home auto-coder-worker
COPY --from=python-environment /opt/auto-coder/.venv /opt/auto-coder/.venv
ENV PATH="/opt/auto-coder/.venv/bin:/home/node/.opencode/bin:${PATH}"
# Embed the trusted build input so the installed artifact reports it without Docker/Git/network access.
RUN python -m auto_coder.build_provenance embed
COPY --from=build /usr/local/bin/opencode /usr/local/bin/opencode
COPY --from=build /usr/local/bin/codex /usr/local/bin/codex
WORKDIR /workspace
ENTRYPOINT ["auto-coder"]
