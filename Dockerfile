FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE MANIFEST.in ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

ARG TARGETARCH
ARG OPENCODE_VERSION=1.18.31
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && \
    ARCH="${TARGETARCH:-$(dpkg --print-architecture)}" && \
    case "$ARCH" in \
        amd64) OPENCODE_ARCH="x64" ;; \
        arm64) OPENCODE_ARCH="arm64" ;; \
        *) echo "Unsupported architecture for OpenCode: $ARCH" >&2; exit 1 ;; \
    esac && \
    curl -fsSL "https://github.com/anomalyco/opencode/releases/download/v${OPENCODE_VERSION}/opencode-linux-${OPENCODE_ARCH}.tar.gz" -o /tmp/opencode.tar.gz && \
    tar -xzf /tmp/opencode.tar.gz -C /usr/local/bin opencode && \
    chmod +x /usr/local/bin/opencode && \
    rm -f /tmp/opencode.tar.gz && \
    rm -rf /var/lib/apt/lists/*

FROM python:3.12-slim
ARG AUTO_CODER_SOURCE_REVISION=unknown
LABEL org.opencontainers.image.revision=$AUTO_CODER_SOURCE_REVISION
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels
COPY --from=build /usr/local/bin/opencode /usr/local/bin/opencode
WORKDIR /workspace
ENTRYPOINT ["auto-coder"]
