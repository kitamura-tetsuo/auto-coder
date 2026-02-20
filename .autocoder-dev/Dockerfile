FROM ubuntu:22.04

# Build arguments
ARG AUTHORIZED_KEYS=""
ARG DOCKER_GID=999

# Avoid prompts
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    git \
    sudo \
    openssh-server \
    gnupg2 \
    ca-certificates \
    software-properties-common \
    pipx \
    && rm -rf /var/lib/apt/lists/*

# Install Docker CLI
RUN install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null \
    && apt-get update \
    && apt-get install -y docker-ce-cli

# Install Python 3.12
RUN add-apt-repository ppa:deadsnakes/ppa \
    && apt-get update \
    && apt-get install -y python3.12 python3.12-venv python3.12-dev \
    && ln -sf /usr/bin/python3.12 /usr/bin/python3

# Install Node.js (required for gemini CLI)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g @google/gemini-cli

# Install gh CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update \
    && apt-get install gh -y

# Install Tailscale
RUN curl -fsSL https://tailscale.com/install.sh | sh

# Install uv
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

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV UV_SYSTEM_PYTHON=1
ENV PATH="/home/node/.local/bin:${PATH}"

WORKDIR /workspace

# Install auto-coder via pipx (as node user)
# USER node
RUN pipx install --python python3.12 git+https://github.com/kitamura-tetsuo/auto-coder.git && \
    pipx ensurepath

# Note: uv sync will be run at runtime via entrypoint if needed.

USER root
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["/bin/bash"]
