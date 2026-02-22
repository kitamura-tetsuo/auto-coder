FROM ubuntu:22.04

# Build arguments
ARG AUTHORIZED_KEYS=""
ARG DOCKER_GID=999

# Avoid prompts
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV UV_SYSTEM_PYTHON=1
ENV PATH="/root/.local/bin:/home/node/.local/bin:${PATH}"

# Install all system dependencies in a consolidated layer to minimize image size
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    sudo \
    openssh-server \
    gnupg2 \
    ca-certificates \
    software-properties-common \
    && add-apt-repository ppa:deadsnakes/ppa \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update && apt-get install -y --no-install-recommends \
    docker-ce-cli \
    python3.12 python3.12-venv python3.12-dev build-essential \
    nodejs \
    gh \
    && curl -fsSL https://tailscale.com/install.sh | sh \
    && ln -sf /usr/bin/python3.12 /usr/bin/python3 \
    && ln -sf /usr/bin/python3 /usr/bin/python \
    && npm install -g @google/gemini-cli \
    && npm cache clean --force \
    && apt-get purge -y --auto-remove software-properties-common \
    && rm -rf /var/lib/apt/lists/*

# Install uv from official image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Setup user 'node'
RUN groupadd -g $DOCKER_GID docker_host || true && \
    useradd -m -s /bin/bash node && \
    echo "node ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers && \
    usermod -aG sudo node && \
    (getent group docker && usermod -aG docker node || true) && \
    (getent group docker_host && usermod -aG docker_host node || true)

# Setup SSH
RUN mkdir -p /var/run/sshd /root/.ssh /home/node/.ssh && \
    echo 'AuthorizedKeysFile .ssh/authorized_keys .ssh-local/authorized_keys' >> /etc/ssh/sshd_config && \
    if [ -n "$AUTHORIZED_KEYS" ]; then \
    echo "$AUTHORIZED_KEYS" >> /root/.ssh/authorized_keys && \
    echo "$AUTHORIZED_KEYS" >> /home/node/.ssh/authorized_keys && \
    chmod 600 /root/.ssh/authorized_keys && \
    chown node:node /home/node/.ssh/authorized_keys && \
    chmod 600 /home/node/.ssh/authorized_keys; \
    fi

WORKDIR /workspaces

# Install torch-cpu separately to optimize layer caching
RUN uv pip install --no-cache --system \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    "torch"

# Install auto-coder and watcher
RUN uv pip install --no-cache --system \
    "git+https://github.com/kitamura-tetsuo/auto-coder.git" \
    "git+https://github.com/kitamura-tetsuo/auto-coder.git#subdirectory=scripts/process_issues_watcher"

USER root
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["/bin/bash"]
