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

FROM python:3.12-slim AS build
ARG TARGETARCH
ARG OPENCODE_VERSION=1.18.31
ARG CODEX_VERSION=0.159.2
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && \
    ARCH="${TARGETARCH:-$(dpkg --print-architecture)}" && \
    case "$ARCH" in \
        amd64) OPENCODE_ARCH="x64"; CODEX_ARCH="x86_64"; CODEX_NPM_ARCH="x64" ;; \
        arm64) OPENCODE_ARCH="arm64"; CODEX_ARCH="aarch64"; CODEX_NPM_ARCH="arm64" ;; \
        *) echo "Unsupported architecture for OpenCode: $ARCH" >&2; exit 1 ;; \
    esac && \
    if curl --retry 2 --retry-all-errors --retry-delay 3 --connect-timeout 15 --max-time 90 -fsSL "https://github.com/anomalyco/opencode/releases/download/v${OPENCODE_VERSION}/opencode-linux-${OPENCODE_ARCH}.tar.gz" -o /tmp/opencode.tar.gz; then \
        tar -xzf /tmp/opencode.tar.gz -C /usr/local/bin opencode && \
        chmod +x /usr/local/bin/opencode; \
    else \
        curl --retry 3 --retry-all-errors --retry-delay 3 --connect-timeout 15 --max-time 90 -fsSL "https://registry.npmjs.org/opencode-linux-${OPENCODE_ARCH}/-/opencode-linux-${OPENCODE_ARCH}-${OPENCODE_VERSION}.tgz" -o /tmp/opencode-npm.tgz && \
        tar -xzf /tmp/opencode-npm.tgz -C /usr/local/bin --strip-components=2 package/bin/opencode && \
        chmod +x /usr/local/bin/opencode; \
    fi && \
    test "$(opencode --version)" = "${OPENCODE_VERSION}" && \
    if curl --retry 2 --retry-all-errors --retry-delay 3 --connect-timeout 15 --max-time 90 -fsSL "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-${CODEX_ARCH}-unknown-linux-musl.tar.gz" -o /tmp/codex.tar.gz; then \
        tar -xzf /tmp/codex.tar.gz -C /tmp && \
        mv "/tmp/codex-${CODEX_ARCH}-unknown-linux-musl" /usr/local/bin/codex; \
    else \
        curl --retry 3 --retry-all-errors --retry-delay 3 --connect-timeout 15 --max-time 90 -fsSL "https://registry.npmjs.org/@openai/codex/-/codex-${CODEX_VERSION}-linux-${CODEX_NPM_ARCH}.tgz" -o /tmp/codex-npm.tgz && \
        tar -xzf /tmp/codex-npm.tgz -C /usr/local/bin --strip-components=4 "package/vendor/${CODEX_ARCH}-unknown-linux-musl/bin/codex"; \
    fi && \
    chmod +x /usr/local/bin/codex && \
    test "$(codex --version)" = "codex-cli ${CODEX_VERSION}" && \
    rm -f /tmp/opencode.tar.gz /tmp/opencode-npm.tgz /tmp/codex.tar.gz /tmp/codex-npm.tgz && \
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
