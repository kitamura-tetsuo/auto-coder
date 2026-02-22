# Auto-Coder Standalone Environment

This directory contains the Docker environment for running `auto-coder` in a dedicated container, separate from your target application's development environment.

## Motivation
Running `auto-coder` in its own container avoids the overhead of installing its extensive dependencies into every project. It uses **Docker-outside-of-Docker (DooD)** to interact with your target development containers.

## Prerequisites
- Docker and Docker Compose installed on the host.
- A target development container running the project you want to modify.

## Getting Started

1. **Configure Environment Variables**
   Create a `.autocoder-dev/docker-compose.override.yml` or a `.env` file:
   ```yaml
   services:
     auto-coder:
       environment:
         - GITHUB_PERSONAL_ACCESS_TOKEN=your_token
         - TS_AUTHKEY=your_tailscale_key (optional)
   ```

2. **Start the Environment**
   Run the provided `run.sh` script. It will automatically collect your host's SSH public keys (`~/.ssh/*.pub`) and inject them into the container so you can SSH into it.
   ```bash
   cd .autocoder-dev
   ./run.sh
   ```

3. **Install Dependencies (First time only)**
   ```bash
   ./setup.sh
   ```

4. **Run Auto-Coder**
   You can run `auto-coder` inside the container:
   ```bash
   docker exec -it auto-coder-env auto-coder process-issues --repo owner/repo
   ```

## Remote Test Execution
This setup supports running tests inside your **target development container** while `auto-coder` remains isolated.

- **Redirection**: Both `run_local_tests` and `_run_pr_tests` in the Python source, as well as `scripts/test.sh`, check for the `AM_I_AUTOCODER_CONTAINER` environment variable.
- **Dynamic Container Naming**: When running in this dedicated environment, target containers are automatically expected to be named `auto-coder-[repo-name]`. For example, if processing `owner/my-project`, the target container must be named `auto-coder-my-project`.
- **Mechanism**: `auto-coder` uses `docker exec` to run the test script inside the development container associated with the repository.
- **Benefits**: Tests run in their native environment with project-specific dependencies, while `auto-coder`'s environment remains clean.
